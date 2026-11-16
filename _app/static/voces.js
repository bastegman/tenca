/* Quién habla (voces): etiquetas de hablante en la transcripción, popover para nombrar / recordar / fusionar,
   barra de estado en la reunión y el apartado «Voces» de Ajustes. Todo contra /api/voces* y /api/hablantes. */
S.spk = null;  // { path, mtime, exists, hablantes, tu, who[], n, known, personas, usuario, enabled, job }

const spkInfo = h => (S.spk && S.spk.hablantes && S.spk.hablantes[h]) || null;
const spkLabel = h => h === 'varios' ? 'varios' : (spkInfo(h) || {}).label || (h ? 'Persona ' + h.replace(/^h/, '') : '');
function spkChip(h) {
  if (!h) return '';
  const i = spkInfo(h), cls = h === 'varios' ? ' many' : i && i.usuario && !i.provisional ? ' me' : i && i.breve ? ' brief' : i && i.provisional ? ' doubt' : i && i.persona ? ' known' : '';
  return `<button class="spk${cls}${/^h\d+$/.test(h) ? ' c' + ((parseInt(h.slice(1), 10) - 1) % 5 + 1) : ''}" data-spk="${esc(h)}" title="${h === 'varios' ? 'Hablan varias personas a la vez' : 'Quién habla · clic para poner nombre, recordar o corregir'}">${esc(spkLabel(h))}</button>`;
}
const fmtSecs = s => s >= 60 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`;

async function loadSpeakers(a) {
  if (!a) return;
  const want = a.path;
  let r;
  try { r = await api('/api/hablantes?path=' + enc(a.path)); } catch { r = { exists: false }; }
  if (S.route.kind !== 'a' || S.route.path !== want) return;
  const hadWho = S.spk && S.spk.path === want && JSON.stringify(S.spk.who || []);
  S.spk = { path: want, mtime: a.spk_mtime, ...r };
  renderVoicesBar(a);
  if (S.tx && S.tx.path === want && hadWho !== JSON.stringify(S.spk.who || []) && !S.editing) renderTxBody();
}

function vozJobFor(path) { return S.jobs.find(j => j.path === path && j.kind === 'voces' && isActive(j)); }
function renderVoicesBar(a) {
  const el = $('#aVoz');
  if (!el) return;
  const j = vozJobFor(a.path), s = S.spk && S.spk.path === a.path ? S.spk : null;
  const last = S.jobs.filter(x => x.path === a.path && x.kind === 'voces').pop();
  const on = S.config.voices && S.config.voices.enabled;
  if (S.irQuien === a.path && s && !s.exists) S.irQuien = null;  // sin voces: «Ponles nombre» no tiene bloque al que ir
  let html = '';
  if (j) {
    html = `<span class="vz-st">${I.voice}${esc(j.status === 'queued' ? 'En cola para identificar voces' : j.stage)}${j.status === 'running' ? ` (${Math.round(j.progress * 100)} %)` : ''}</span>
      <span class="sp"></span><button class="btn ghost sm danger" data-act="cancel-job" data-id="${j.id}">Cancelar</button>`;
  } else if (s && s.exists) {
    // B8 (punto 21 del review): el bloque «Quién habla» se despliega aquí mismo, en la página, sin diálogo ni
    // ventana; plegado es esta línea
    const sinNombre = qhFilas(s).filter(f => !f.nombre).length;
    if (S.irQuien === a.path) { S.qhAbierto = a.path; S.irQuien = null; S.qhIr = true; }
    const abierto = S.qhAbierto === a.path;
    // mientras se graba con el directo en la nube (punto 56): identificadas por bloques de 30 min
    const parcial = s.parcial ? ` · hasta el min ${Math.round(s.parcial / 60)} (el resto, al terminar)` : '';
    html = `<span class="vz-st">${I.voice}Voces: ${s.n} ${s.n === 1 ? 'persona' : 'personas'} · ${s.known} reconocida${s.known === 1 ? '' : 's'}${Object.values(s.hablantes || {}).some(x => x.usuario && !x.provisional) ? ' · Tú' : ''}${parcial}</span>
      <button class="btn sm${sinNombre && !abierto ? ' primary' : ''}" type="button" data-act="qh-abrir" aria-expanded="${abierto}" aria-controls="qhBloque">${abierto ? 'Plegar' : sinNombre ? `Ponles nombre (${sinNombre})` : 'Quién habla'}</button>
      <span class="sp"></span>
      ${S.tx && S.tx.kind === 'srt' ? `<button class="btn ghost sm" data-act="voz-export" title="Guarda <reunión>.hablantes.txt junto al audio">${I.text}Exportar con hablantes</button>` : ''}
      ${on ? `<button class="btn ghost sm" data-act="voz-identify" title="Repetir la identificación">${I.refresh}Volver a identificar</button>` : ''}
      ${abierto ? qhBloqueHtml(a, s) : ''}`;
  } else if (on && (a.txt || a.srt) && !a.recording && !jobFor(a.path)) {
    html = `${last && last.status === 'error' ? `<span class="vz-st err">No se pudieron identificar las voces: ${esc(last.aviso?.frase || last.error || '')}</span>` : '<span class="muted">' + I.voice + 'Quién habla: aún sin identificar</span>'}
      <span class="sp"></span><button class="btn sm" data-act="voz-identify">${I.voice}Identificar voces</button>`;
  }
  // con el bloque abierto y algo escrito en él, no se repinta (los trabajos refrescan la barra cada pocos segundos)
  // si es la misma identificación (mismo `creado`) y sin trabajo de voces, no se repinta con algo escrito; si las voces
  // se han vuelto a identificar, sí: sus filas podrían ser ya otras voces (revisión 1, P0-1)
  const qh = $('#qhBloque', el), misma = qh && s && qh.dataset.creado === String(s.creado ?? '');
  // mientras se vuelven a leer las voces (S.spk a null tras guardar o juntar), un refresco de trabajos no quita el
  // bloque abierto: se perdería lo escrito en las demás filas (revisión 2, N6)
  if (qh && !s && !j && S.qhAbierto === a.path) return;
  if (qh && misma && !j && S.qhAbierto === a.path && (qh.contains(document.activeElement) || qh.dataset.sucio || qh.dataset.busy)) return;
  const borrador = misma ? qhBorrador(qh) : null;  // lo escrito en las demás filas no se pierde al guardar una (P1-2)
  el.innerHTML = html ? `<div class="card voz-bar${S.qhAbierto === a.path ? ' abierto' : ''}">${html}</div>` : '';
  if (borrador && $('#qhBloque', el)) qhReponer($('#qhBloque', el), borrador);
  if (S.qhIr && $('#qhBloque', el)) {
    S.qhIr = false;
    const b = $('#qhBloque', el);
    $('.voz-bar', el).scrollIntoView({ block: 'start' });
    ($('[data-qh-nombre]:not([value]), [data-qh-nombre][value=""]', b) || $('[data-qh-nombre]', b))?.focus({ preventScroll: true });
  }
}

/* ---------------------------------------------------------------- B8: el bloque «Quién habla» */
function qhBorrador(b) {
  const filas = {};
  $$('.qh-fila', b).forEach(f => {
    const n = $('[data-qh-nombre]', f);
    filas[f.dataset.h] = { v: n ? n.value : null, ini: n ? n.defaultValue : null, rec: !!$('[data-qh-rec]', f)?.checked, sel: !!$('[data-qh-sel]', f)?.checked };
  });
  return { filas, con: !!$('#qhConsent', b)?.checked, guardada: S.qhGuardada || null };
}
function qhReponer(b, d) {
  $$('.qh-fila', b).forEach(f => {
    const x = d.filas[f.dataset.h];
    if (!x || f.dataset.h === d.guardada) return;
    const n = $('[data-qh-nombre]', f);
    if (n && x.v !== x.ini) { n.value = x.v; b.dataset.sucio = '1'; }
    if ($('[data-qh-rec]', f)) $('[data-qh-rec]', f).checked = x.rec;
    $('[data-qh-sel]', f).checked = x.sel;
  });
  $('#qhConsent', b).checked = d.con;
  $('.qh-consent', b).classList.toggle('hidden', !$$('[data-qh-rec]', b).some(x => x.checked));
  $('[data-qh-unir]', b).disabled = $$('[data-qh-sel]', b).filter(x => x.checked).length < 2;
  if (d.guardada) {  // tras guardar una, el foco a la siguiente sin nombre
    S.qhGuardada = null;
    ($$('[data-qh-nombre]', b).find(x => !x.value.trim()) || $('[data-qh-unir]', b))?.focus({ preventScroll: true });
  }
}
/* todas las voces, de la que más habla a la que menos, cada una con su nombre (si lo tiene) */
function qhFilas(s) {
  const personas = s.personas || [];
  return Object.entries(s.hablantes || {}).filter(([h]) => h !== 'varios')
    .sort((x, y) => (y[1].segundos || 0) - (x[1].segundos || 0)).map(([h, i]) => {
      const p = i.persona ? personas.find(x => x.id === i.persona) : null;
      return { h, i, persona: p, tu: !!(i.usuario && !i.provisional), nombre: i.usuario && !i.provisional ? 'Tú' : p && !i.provisional ? p.nombre : '' };
    });
}
/* nombres para sugerir: los participantes de la reunión (los de la cita de Outlook se guardan ahí, como sugeridos)
   y las personas ya guardadas, sin los que ya tienen voz en esta reunión */
function qhSugerencias(a, s, filas) {
  const usados = new Set(filas.map(f => fold(f.nombre)).filter(Boolean));
  const lista = String((a.props || {}).participantes || ''), sep = lista.includes(';') ? ';' : ',';
  const part = lista.split(sep).map(x => x.trim()).filter(Boolean);
  const out = [], vistos = new Set();
  for (const n of [...part, ...(s.personas || []).filter(p => !p.es_usuario).map(p => p.nombre)]) {
    const k = fold(n);
    if (!vistos.has(k) && !usados.has(k)) { vistos.add(k); out.push(n); }
  }
  return { todas: out, citas: part.filter(n => !usados.has(fold(n))), origen: (a.props || {}).participantes_origen };
}
function qhBloqueHtml(a, s) {
  const filas = qhFilas(s), sug = qhSugerencias(a, s, filas);
  const de = sug.origen === 'outlook' ? 'de la cita de Outlook' : sug.origen === 'teams' ? 'de Teams' : 'de la reunión';
  return `<div class="qh" id="qhBloque" role="group" aria-label="Quién habla" data-creado="${esc(String(s.creado ?? ''))}">
    ${sug.citas.length ? `<div class="qh-sug"><span class="muted">Asistentes ${de}:</span>${sug.citas.map(n => `<button type="button" class="tag-chip qh-chip" data-qh-sug="${esc(n)}" title="Ponerlo en la voz que estás nombrando">${esc(n)}</button>`).join('')}</div>` : ''}
    <div class="qh-lista" role="list">${filas.map(f => `<div class="qh-fila" role="listitem" data-h="${esc(f.h)}">
      <input type="checkbox" data-qh-sel aria-label="Elegir ${esc(spkLabel(f.h))} para juntarla con otras">
      <span class="qh-quien">${spkChip(f.h)}<span class="qh-t muted">${fmtSecs(f.i.segundos || 0)}${f.i.breve ? ' · habla poco' : ''}</span></span>
      <button type="button" class="btn sm ghost qh-oir" data-qh-oir aria-label="Oír 5 segundos de ${esc(spkLabel(f.h))}" title="Oír unos 5 segundos de esta voz">${I.play}Oír</button>
      ${f.tu ? `<span class="qh-yo muted">Tú <small>(para cambiarlo, clic en la etiqueta)</small></span>`
        : `<input class="input qh-nombre" data-qh-nombre value="${esc(f.nombre)}" placeholder="¿Quién es?" list="qhSugs" autocomplete="off" spellcheck="false" aria-label="Nombre de ${esc(spkLabel(f.h))}">
      <label class="chk qh-rec"><input type="checkbox" data-qh-rec> Recordar</label>
      <button type="button" class="btn sm" data-qh-guardar>Guardar</button>`}
    </div>`).join('')}</div>
    <datalist id="qhSugs">${sug.todas.map(n => `<option value="${esc(n)}">`).join('')}</datalist>
    <label class="chk sub qh-consent hidden"><input type="checkbox" id="qhConsent"> Tengo el consentimiento de las personas que marco con «Recordar» para guardar su huella de voz en este PC</label>
    <div class="qh-pie"><button type="button" class="btn sm" data-qh-unir disabled>Son la misma persona</button>
      <span class="muted qh-ayuda">Marca dos o más voces que la app ha separado y júntalas en una.</span></div>
    <div class="sp-estado hidden" role="status" aria-live="polite"></div>
  </div>`;
}
/* oír ~5 s de una voz: su frase más larga de la transcripción (who[] dice quién dice cada frase) */
function qhMuestra(h) {
  const tx = S.tx, who = typeof whoFor === 'function' ? whoFor(tx) : null;
  if (!tx || !who) return null;
  let best = null;
  tx.segs.forEach((g, k) => {
    if (who[k] !== h) return;
    const d = (g.end ?? g.start) - (g.start ?? 0);
    if (!best || d > best.d) best = { t: g.start ?? 0, d };
  });
  return best;
}
/* la parada de «Oír»: al llegar al final de la muestra; se olvida si el audio cambia de reunión o de sitio (revisión 1,
   P1-4: un temporizador de 5 s pausaba lo que se pusiera a sonar después) */
let qhOyendo = null;
audio.addEventListener('timeupdate', () => {
  const o = qhOyendo;
  if (!o) return;
  if (S.playing !== o.path || audio.currentTime < o.t - 1 || audio.currentTime > o.fin + 1.5) { qhOyendo = null; return; }
  if (audio.currentTime >= o.fin) { qhOyendo = null; audio.pause(); }
});
// un salto a otro sitio (otra frase, el reproductor) mientras suena la muestra: ya no es la muestra (revisión 2, N5)
audio.addEventListener('seeking', () => { if (qhOyendo && Math.abs(audio.currentTime - qhOyendo.t) > 0.25) qhOyendo = null; });
async function qhGuardarFila(a, fila, bloque) {
  const h = fila.dataset.h, s = S.spk, i = spkInfo(h) || {};
  const nombre = $('[data-qh-nombre]', fila).value.trim();
  if (!nombre) return toast('Escribe un nombre');
  const rec = $('[data-qh-rec]', fila).checked, con = $('#qhConsent', bloque).checked;
  const cur = i.persona ? (s.personas || []).find(p => p.id === i.persona) : null;
  // el `creado` con el que se pintó el bloque: si las voces se han vuelto a identificar, el servidor lo rechaza
  const r = await api('/api/voces/persona', spkNameBody(a, h, cur, nombre, { recordar: rec, consentimiento: con, creado: bloque.dataset.creado || s.creado }));
  S.qhGuardada = h;
  // el consentimiento vale para ese guardado: la siguiente persona con «Recordar» lo pide otra vez (revisión 2, N4)
  if (rec) $('#qhConsent', bloque).checked = false;
  return rec ? `🗣 ${esc(nombre)}: huella guardada (${r.huellas})` : `🗣 ${esc(nombre)} en esta reunión`;
}
async function qhAccion(e) {
  const bloque = e.target.closest('#qhBloque'), a = curAudio(), s = S.spk;
  if (!bloque || !a || !s || s.path !== a.path) return;
  const fila = e.target.closest('.qh-fila'), estado = $('.sp-estado', bloque);
  const ocupado = txt => { bloque.dataset.busy = '1'; $$('button, input', bloque).forEach(x => { x.dataset.was = x.disabled ? '1' : ''; x.disabled = true; }); estado.textContent = txt; estado.classList.remove('hidden'); };
  const libre = () => { delete bloque.dataset.busy; $$('button, input', bloque).forEach(x => { x.disabled = x.dataset.was === '1'; }); estado.classList.add('hidden'); };
  const fin = async msg => { delete bloque.dataset.busy; delete bloque.dataset.sucio; if (bloque.contains(document.activeElement)) document.activeElement.blur(); S.spk = null; await loadSpeakers(a); if (msg) toast(msg); };
  const sug = e.target.closest('[data-qh-sug]');
  if (sug) {
    const campo = S.qhCampo && bloque.contains(S.qhCampo) ? S.qhCampo : $$('[data-qh-nombre]', bloque).find(x => !x.value.trim());
    if (!campo) return toast('Todas las voces tienen ya un nombre');
    campo.value = sug.dataset.qhSug; bloque.dataset.sucio = '1'; campo.focus();
    return;
  }
  if (e.target.closest('[data-qh-oir]') && fila) {
    // unos segundos de esa voz sola, de los tramos de su sidecar (el servidor; no hace falta el .srt). Si no contesta
    // o las voces se han vuelto a identificar, la frase más larga de la transcripción
    let m = null;
    try {
      const r = await api(`/api/hablantes/muestra?path=${encodeURIComponent(a.path)}&h=${encodeURIComponent(fila.dataset.h)}`, undefined, 4000);
      if (r.fin > r.inicio && (!r.creado || !s.creado || r.creado === s.creado)) m = { t: r.inicio, d: r.fin - r.inicio };
    } catch { /* la transcripción */ }
    if (curAudio()?.path !== a.path) return;  // se ha cambiado de reunión mientras
    if (!m && (!S.tx || S.tx.path !== a.path)) return toast('Un momento: aún se está cargando la transcripción');
    m ||= qhMuestra(fila.dataset.h);
    if (!m) return toast('No sé en qué minuto habla esta voz: hace falta la transcripción con tiempos (.srt)');
    loadAudio(a, m.t, true);
    qhOyendo = { path: a.path, t: m.t, fin: m.t + Math.min(5, Math.max(1, m.d)) };
    return;
  }
  if (e.target.closest('[data-qh-guardar]') && fila) {
    if (bloque.dataset.busy) return;
    const rec = $('[data-qh-rec]', fila).checked;
    if (!$('[data-qh-nombre]', fila).value.trim()) return toast('Escribe un nombre');
    if (rec && !$('#qhConsent', bloque).checked) {  // antes de «Guardando…»: sin consentimiento no se manda nada
      $('.qh-consent', bloque).classList.remove('hidden');
      return toast('Para guardar la huella marca la casilla de consentimiento', 'err');
    }
    ocupado(rec ? 'Guardando la huella de voz…' : 'Guardando…');
    try { const msg = await qhGuardarFila(a, fila, bloque); if (!msg || typeof msg !== 'string') return libre(); await fin(msg); } catch (err) { libre(); toast(esc(err.message), 'err'); }
    return;
  }
  if (e.target.closest('[data-qh-unir]')) {
    if (bloque.dataset.busy) return;
    const hs = $$('.qh-fila', bloque).filter(f => $('[data-qh-sel]', f).checked).map(f => f.dataset.h);
    if (hs.length < 2) return;
    const con = hs[0];  // la que más habla (la lista va por tiempo): las demás se juntan en ella
    ocupado('Juntando…');
    // tras juntar, ninguna queda marcada: la voz que queda no se iría con la siguiente unión (revisión 2, N1)
    $$('[data-qh-sel]', bloque).forEach(x => { x.checked = false; });
    let hechas = 0;
    try {
      for (const h of hs.slice(1)) { await api('/api/hablantes/unir', spkJoinBody(a, h, con, bloque.dataset.creado || s.creado)); hechas++; }
      await fin(`${hs.length} voces juntadas en ${esc(spkLabel(con))}`);
    } catch (err) { await fin(); toast(`${hechas ? `Se juntaron ${hechas + 1} de ${hs.length}. ` : ''}${esc(err.message)}`, 'err'); }
  }
}
main.addEventListener('click', e => { if (e.target.closest('#qhBloque')) qhAccion(e); });
main.addEventListener('focusin', e => { if (e.target.matches && e.target.matches('#qhBloque [data-qh-nombre]')) S.qhCampo = e.target; });
main.addEventListener('input', e => { const b = e.target.closest && e.target.closest('#qhBloque'); if (b && e.target.matches('[data-qh-nombre]')) b.dataset.sucio = '1'; });
main.addEventListener('change', e => {
  const b = e.target.closest && e.target.closest('#qhBloque');
  if (!b) return;
  if (e.target.matches('[data-qh-rec]')) b.querySelector('.qh-consent').classList.toggle('hidden', !$$('[data-qh-rec]', b).some(x => x.checked));
  if (e.target.matches('[data-qh-sel]')) $('[data-qh-unir]', b).disabled = $$('[data-qh-sel]', b).filter(x => x.checked).length < 2;
});
main.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.matches && e.target.matches('#qhBloque [data-qh-nombre]')) { e.preventDefault(); e.target.closest('.qh-fila').querySelector('[data-qh-guardar]')?.click(); }
});
/* Escape cierra el globo de un hablante (app.js lo llama; uno guardándose no se cierra) */
function spkPopCerrar() {
  const p = $('.spk-pop');
  if (!p || p.dataset.busy) return false;
  const ancla = p._ancla;
  closeSpkPop();
  if (ancla && ancla.isConnected) ancla.focus({ preventScroll: true });
  return true;
}

/* trabajos de voces: al terminar, recargar las etiquetas de la reunión abierta (lo llama onJobs de app.js) */
function onVoicesJobs(prev) {
  const a = curAudio();
  for (const j of S.jobs) {
    if (j.kind !== 'voces') continue;
    const p = prev.get(j.id);
    if (p && p.status !== j.status && !isActive(j) && a && a.path === j.path) { S.spk = null; loadSpeakers(a); }
  }
  if (a && $('#aVoz')) renderVoicesBar(a);
}

/* ---------------------------------------------------------------- popover de un hablante */
/* Lo que se manda al servidor desde la etiqueta (funciones puras, probadas en tests/test_fix_voces.py). Escribir un
   nombre dice QUIÉN es este hablante (alguien que ya se llama así, o una persona nueva): nunca renombra a la persona
   que tenía salvo que se marque «solo corrijo el nombre», y nunca a la tuya (msg 1170: «asume que soy yo»). */
// `creado`: la identificación sobre la que se abrió la etiqueta (si se volvió a identificar, «h2» puede ser otra voz)
function spkNameBody(a, h, cur, nombre, opts) {
  const own = cur && !cur.es_usuario;
  return { id: own ? cur.id : undefined, renombrar: !!(own && opts.corregir), path: a.path, h, nombre, recordar: !!opts.recordar, consentimiento: !!opts.consentimiento, creado: opts.creado };
}
function spkMeBody(a, h, s, recordar) {
  // sin el nombre del cuadro (puede ser el de la persona que ya tenía): tu persona se llama «Yo» hasta que la cambies
  return { path: a.path, h, es_usuario: true, recordar: !!recordar, consentimiento: !!recordar, creado: s.creado };
}
function spkPickBody(a, h, i, target, creado) {
  // «Es…»: un hablante sin persona o que eres tú se reasigna. «Fusionar con…» solo si ya es de otra persona, y nunca
  // con la tuya (dentro de otra, esa pasaría a ser «Tú» con tus huellas; al revés, tú te quedarías con su voz)
  return i.persona && !i.usuario ? ['/api/voces/fusionar', { id_a: target, id_b: i.persona }] : ['/api/voces/reasignar', { path: a.path, h, persona: target, creado }];
}

// «Es el mismo que…» (msg 1242): la app partió una voz en dos hablantes de esta reunión
function spkJoinBody(a, h, con, creado) { return { path: a.path, h, con, creado }; }
function spkJoinOptions(hablantes, h, label) {
  return Object.entries(hablantes || {}).filter(([k]) => k !== h && k !== 'varios')
    .sort((x, y) => (y[1].segundos || 0) - (x[1].segundos || 0)).map(([k, x]) => ({ h: k, text: `${label(k)} · ${fmtSecs(x.segundos || 0)}` }));
}

function closeSpkPop() { $$('.spk-pop').forEach(p => p.remove()); }
function openSpeakerPop(h, anchor) {
  // uno guardándose (fix-lento) no se cierra ni se cambia por otro: el aviso de cómo ha ido sale en él
  if ($('.spk-pop[data-busy]')) return toast('Espera a que termine de guardar');
  closePop(); closeSpkPop();
  const a = curAudio(), s = S.spk;
  if (!a || !s || h === 'varios') return;
  const i = spkInfo(h) || {}, personas = s.personas || [];
  const cur = i.persona ? personas.find(p => p.id === i.persona) : null;
  const other = cur && !cur.es_usuario, doubtOther = i.provisional && other, merge = i.persona && !i.usuario;
  const pop = document.createElement('div');
  pop.className = 'spk-pop';
  pop._ancla = anchor;
  pop.innerHTML = `<div class="sp-h">${esc(spkLabel(h))} <span class="muted">· ${fmtSecs(i.segundos || 0)} de voz${i.score != null ? ` · parecido ${String(Math.round(i.score * 100))} %` : ''}${i.breve ? ' · habla poco' : ''}</span></div>
    ${doubtOther ? `<div class="notice info">Parece <b>${esc(cur.nombre)}</b>, pero no es seguro. Si lo es, pulsa <b>Confirmar</b> (se guarda esta voz como otra huella suya).</div>` : ''}
    ${i.usuario && i.provisional ? '<div class="notice info">Parece tu voz, pero no es seguro. Si eres tú, pulsa <b>Soy yo</b>; si no, escribe su nombre.</div>' : ''}
    ${i.usuario && !i.provisional ? '<div class="notice info">Sale como «Tú» por tu micrófono o tu voz guardada. Si no eres tú, escribe su nombre o pulsa <b>No soy yo</b>.</div>' : ''}
    <label>Nombre</label>
    <div class="row"><input class="input" id="spName" value="${esc(other ? cur.nombre : '')}" placeholder="p. ej. Javier" autocomplete="off" spellcheck="false" list="spNames">
      <datalist id="spNames">${personas.filter(p => !p.es_usuario).map(p => `<option value="${esc(p.nombre)}">`).join('')}</datalist>
      <button class="btn sm primary" data-sa="name">${doubtOther ? 'Confirmar' : 'Guardar'}</button></div>
    ${other && !i.provisional ? `<label class="chk sub hidden" id="spFixRow"><input type="checkbox" id="spFix"> Es la misma persona: solo corrijo el nombre de «${esc(cur.nombre)}» (en todas sus reuniones)</label>` : ''}
    <label class="chk"><input type="checkbox" id="spRem" ${doubtOther ? 'checked' : ''}> Recordar en próximas reuniones <span class="muted">(guarda una huella de su voz, solo en este PC)</span></label>
    <label class="chk sub"><input type="checkbox" id="spConsent"> Tengo el consentimiento de esta persona para guardar su huella de voz en este PC</label>
    <div class="row sp-actions">
      <button class="btn sm${i.usuario && !i.provisional ? ' on' : ''}" data-sa="me" title="Este hablante soy yo${s.usuario ? '' : ' (se crea tu persona para reconocerte también en reuniones presenciales)'}">${i.usuario && !i.provisional ? '✓ Soy yo' : 'Soy yo'}</button>
      <select id="spMerge" class="sm" title="${merge ? 'Si esta persona y otra ya guardada son la misma' : 'Este hablante es una persona ya guardada'}"><option value="">${merge ? 'Fusionar con…' : 'Es…'}</option>${personas.filter(p => p.id !== i.persona && !(merge && p.es_usuario)).map(p => `<option value="${esc(p.id)}">${esc(p.nombre)}${p.es_usuario ? ' (tú)' : ''}</option>`).join('')}</select>
      ${Object.keys(s.hablantes || {}).length > 1 ? `<select id="spJoin" class="sm" title="La app ha partido esta voz en dos hablantes: se juntan en uno"><option value="">Es el mismo que…</option>${spkJoinOptions(s.hablantes, h, spkLabel).map(o => `<option value="${esc(o.h)}">${esc(o.text)}</option>`).join('')}</select>` : ''}
      ${i.persona || i.usuario ? `<button class="btn sm" data-sa="notme">${i.usuario ? 'No soy yo' : 'No es esta persona'}</button>` : ''}
      ${other ? '<button class="btn sm ghost danger" data-sa="forget" title="Borra su nombre y sus huellas de este PC">Olvidar persona</button>' : ''}
    </div>
    <div class="sp-estado hidden" role="status" aria-live="polite"></div>`;
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - 440)) + 'px';
  pop.style.top = Math.min(r.bottom + 6, innerHeight - 320) + 'px';
  pop.addEventListener('mousedown', e => e.stopPropagation());
  const fuera = () => { if (pop.dataset.busy) document.addEventListener('mousedown', fuera, { once: true }); else closeSpkPop(); };
  setTimeout(() => document.addEventListener('mousedown', fuera, { once: true }), 0);
  const refresh = async msg => { closeSpkPop(); S.spk = null; await loadSpeakers(a); if (msg) toast(msg); };
  const remember = () => $('#spRem', pop).checked;
  const consent = () => $('#spConsent', pop).checked;
  /* «Guardando…» al momento (fix-lento, 10/11/2026, msg 1479: «tarda minutos y no aparece nada»): con «Recordar» la
     huella de voz tarda unos segundos (más con el PC ocupado) y hasta que acababa no se veía nada. Mientras, nada del
     cuadro se puede volver a pulsar; si falla, vuelve como estaba y sale el motivo */
  const estado = $('.sp-estado', pop);
  const ocupado = (btn, txt) => {
    pop.dataset.busy = '1';
    $$('button, select, input', pop).forEach(x => { x.dataset.was = x.disabled ? '1' : ''; x.disabled = true; });
    if (btn) { btn.dataset.label = btn.innerHTML; btn.textContent = txt; }
    estado.textContent = txt;
    estado.classList.remove('hidden');
  };
  const libre = () => {
    if (!pop.dataset.busy) return;
    delete pop.dataset.busy;
    $$('button, select, input', pop).forEach(x => { x.disabled = x.dataset.was === '1'; });
    $$('[data-label]', pop).forEach(b => { b.innerHTML = b.dataset.label; delete b.dataset.label; });
    estado.classList.add('hidden');
  };
  const run = async fn => { if (pop.dataset.busy) return; try { await fn(); } catch (err) { libre(); toast(esc(err.message), 'err'); } };
  const fixRow = $('#spFixRow', pop);
  $('#spName', pop).addEventListener('keydown', e => { if (e.key === 'Enter') $('[data-sa="name"]', pop).click(); });
  $('#spName', pop).addEventListener('input', e => { if (fixRow) fixRow.classList.toggle('hidden', !e.target.value.trim() || e.target.value.trim() === cur.nombre); });
  $('#spMerge', pop).onchange = () => run(async () => {
    const target = $('#spMerge', pop).value;
    if (!target) return;
    const [url, body] = spkPickBody(a, h, i, target, s.creado);
    ocupado(null, url.endsWith('fusionar') ? 'Fusionando…' : 'Guardando…');
    // si falla (una reunión abierta en otro programa), se repinta igual: parte puede haber cambiado, y así se puede
    // volver a elegir la misma persona
    try { await api(url, body); } catch (err) { await refresh(); throw err; }
    await refresh(url.endsWith('fusionar') ? 'Personas fusionadas: las reuniones anteriores ya muestran el nombre único' : 'Hablante asignado');
  });
  const join = $('#spJoin', pop);
  if (join) join.onchange = () => run(async () => {
    if (!join.value) return;
    ocupado(null, 'Guardando…');
    await api('/api/hablantes/unir', spkJoinBody(a, h, join.value, s.creado));
    await refresh('Hablantes unidos');
  });
  pop.addEventListener('click', e => {
    const b = e.target.closest('[data-sa]');
    if (!b) return;
    const act = b.dataset.sa;
    run(async () => {
      if (act === 'name') {
        const nombre = $('#spName', pop).value.trim();
        if (!nombre) return toast(i.usuario ? 'Escribe su nombre (o pulsa «Soy yo» si eres tú)' : 'Escribe un nombre');
        if (remember() && !consent()) return toast('Para guardar la huella marca la casilla de consentimiento', 'err');
        const corregir = !!(fixRow && !fixRow.classList.contains('hidden') && $('#spFix', pop).checked);
        const rec = remember(), con = consent();
        ocupado(b, rec ? 'Guardando la huella de voz…' : 'Guardando…');
        const r = await api('/api/voces/persona', spkNameBody(a, h, cur, nombre, { corregir, recordar: rec, consentimiento: con, creado: s.creado }));
        await refresh(rec ? `🗣 ${esc(nombre)}: huella guardada (${r.huellas}). Se reconocerá en las próximas reuniones.` : corregir ? `🗣 Nombre corregido en todas sus reuniones: ${esc(nombre)}` : `🗣 ${esc(nombre)} en esta reunión. Marca «Recordar» si quieres reconocerle otras veces.`);
      } else if (act === 'me') {
        const rec = remember();
        ocupado(b, rec ? 'Guardando la huella de voz…' : 'Guardando…');
        await api('/api/voces/persona', spkMeBody(a, h, s, rec));
        await refresh(rec ? 'Marcado como «Tú» y huella guardada: también te reconocerá en reuniones presenciales' : 'Marcado como «Tú» en esta reunión');
      } else if (act === 'notme') {
        ocupado(b, 'Guardando…');
        await api('/api/voces/reasignar', { path: a.path, h, persona: null, creado: s.creado });
        await refresh(i.usuario ? 'Ya no sale como «Tú»: vuelve a ser una persona sin nombre en esta reunión' : 'Desasignado: vuelve a ser una persona sin nombre en esta reunión');
      } else if (act === 'forget') {
        if (!confirm(`¿Olvidar a «${cur.nombre}»? Se borran su nombre y sus huellas de voz de este PC. Las reuniones pasadas lo mostrarán como «Persona N».`)) return;
        ocupado(b, 'Olvidando…');
        await api('/api/voces/olvidar', { id: cur.id });
        await refresh('Persona olvidada');
      }
    });
  });
  $('#spName', pop).focus();
}

main.addEventListener('click', async e => {
  const chip = e.target.closest('.spk');
  if (chip) { e.preventDefault(); e.stopPropagation(); openSpeakerPop(chip.dataset.spk, chip); return; }
  if (e.target.closest('[data-act="qh-abrir"]')) {
    const a0 = curAudio();
    if (!a0) return;
    S.qhAbierto = S.qhAbierto === a0.path ? null : a0.path;
    if (S.qhAbierto) S.qhIr = true;
    renderVoicesBar(a0);
    return;
  }
  const act = e.target.closest('[data-act]');
  if (!act || !main.contains(act)) return;
  const a = curAudio();
  if (!a) return;
  if (act.dataset.act === 'voz-identify') {
    try { await api('/api/voces/identificar', { path: a.path }); toast('🗣 Identificando voces en segundo plano (unos 2 s por minuto de reunión)…'); pollJobs(true); }
    catch (err) { toast(esc(err.message), 'err'); }
  } else if (act.dataset.act === 'voz-export') {
    try {
      const r = await api('/api/voces/exportar', { path: a.path });
      toast(`<b>✓</b> Guardado ${esc(r.path.split('/').pop())} junto al audio (clic para abrirlo)`, 'ok', () => api('/api/open', { path: a.path, ext: '.hablantes.txt' }).catch(() => { }));
    } catch (err) { toast(esc(err.message), 'err'); }
  }
});

/* ---------------------------------------------------------------- Ajustes → Voces */
function vocesFieldsetHtml() {
  const v = S.config.voices || {};
  return `<fieldset><legend>${I.voice}Voces (opcional)</legend>
      <label style="display:flex;gap:8px;align-items:center;font-weight:500"><input type="checkbox" id="vzOn" ${v.enabled ? 'checked' : ''}> Reconocer quién habla en cada reunión (en este PC, sin enviar nada fuera)</label>
      <div id="vzModels" class="help" style="margin-top:4px"></div>
      <div id="vzPeople" class="help" style="margin-top:6px">${v.enabled ? 'Cargando…' : 'Al activarlo se descargan dos modelos (≈35 MB, una sola vez) y, al terminar cada transcripción, un proceso de prioridad baja separa las voces (≈2 s por minuto de reunión). En la transcripción verás «Persona 1», «Persona 2»…: ponles nombre y, si tienes su consentimiento, pulsa «Recordar» para reconocerlas en las próximas reuniones.'}</div>
    </fieldset>`;
}

function bindVocesSettings(d) {
  let timer = null;
  const draw = st => {
    const m = $('#vzModels', d), p = $('#vzPeople', d);
    if (!m || !p) return;
    if (!st.enabled) { m.innerHTML = ''; return; }
    if (st.available === false) m.innerHTML = `<span style="color:var(--err)">✖ La librería de voces (sherpa-onnx) no carga en este PC: vuelve a ejecutar INSTALAR.bat. ${esc(st.why || '')}</span>`;
    else if (st.models.downloading) m.innerHTML = `${esc(st.models.stage || 'Descargando modelos…')} ${Math.round((st.models.frac || 0) * 100)} %`;
    else if (st.models.error) m.innerHTML = `<span style="color:var(--err)">✖ No se pudieron descargar los modelos: ${esc(st.models.error)}</span> <button class="btn ghost sm" type="button" id="vzRetry">Reintentar</button>`;
    else if (st.models.ready) m.innerHTML = `<span style="color:var(--ok)">✓ Modelos listos</span> · se guardan en %LOCALAPPDATA%\\AudiosApp\\models\\voces`;
    else m.innerHTML = 'Modelos pendientes de descarga.';
    const ps = st.personas || [];
    p.innerHTML = `${ps.length ? `<table class="vtable vz-table"><tr><th>Persona</th><th>Tú</th><th>Huellas</th><th>Última vez</th><th></th></tr>${ps.map(x => `<tr>
        <td><input class="input sm" data-vzname="${esc(x.id)}" value="${esc(x.nombre)}" spellcheck="false"></td>
        <td>${x.es_usuario ? '✓' : `<button class="btn ghost sm" type="button" data-vzme="${esc(x.id)}" title="Esta persona soy yo">Soy yo</button>`}</td>
        <td>${x.huellas}${x.mezcla ? ' <span class="vz-mix" title="Sus huellas no se parecen entre sí: puede llevar la voz de otra persona. Olvídala y vuelve a guardarla desde una reunión.">⚠</span>' : ''}</td><td>${x.ultima ? esc(x.ultima.slice(0, 10)) : '—'}</td>
        <td><button class="btn ghost sm danger" type="button" data-vzforget="${esc(x.id)}">Olvidar</button></td></tr>`).join('')}</table>
      ${ps.some(x => x.es_usuario && x.mezcla) ? '<div class="help" style="margin-top:6px;color:var(--err)">⚠ Tu persona tiene huellas que no se parecen entre sí: puede llevar la voz de otra persona (pasaba al poner nombre a un «Tú» antes del 07/11/2026). Mientras, tu voz no se reconoce sola: se pregunta. Pulsa «Olvidar» en tu fila y, en tu próxima reunión, «Soy yo» con «Recordar».</div>' : ''}
      <div class="row" style="margin-top:8px"><button class="btn ghost sm danger" type="button" id="vzForgetAll">Olvidar todas</button></div>` : '<div>Todavía no hay personas guardadas: aparecerán cuando pongas nombre a un hablante en una reunión y pulses «Recordar».</div>'}
      <div style="margin-top:8px">Qué se guarda y dónde: por reunión, <code>&lt;grabación&gt;.hablantes.json</code> junto al audio (tramos y «Persona N», sin datos de voz); las huellas de voz (vectores de 192 números por persona, máximo 5) solo en <code>%LOCALAPPDATA%\\AudiosApp\\voces.json</code>, cifrado con la cuenta de Windows y fuera de OneDrive. Son datos biométricos: guarda huellas solo con el consentimiento de cada persona; olvidarlas las borra de verdad. Nada se envía a la nube${st.store_error ? `<br><span style="color:var(--err)">voces.json no se pudo leer y se apartó (${esc(st.store_error)})</span>` : ''}.</div>`;
  };
  const poll = async () => {
    clearTimeout(timer);
    if (!document.body.contains(d)) return;
    try { const st = await api('/api/voces'); draw(st); if (st.enabled && st.models.downloading) timer = setTimeout(poll, 1500); } catch { }
  };
  if (S.config.voices && S.config.voices.enabled) poll();
  $('#vzOn', d).onchange = async e => {
    const on = e.target.checked;
    if (on) {
      e.target.checked = false;
      const ok = await ask('Reconocer quién habla', `<p>${APP_NAME} separará las voces de cada reunión <b>en este PC</b> (nada sale a internet) y, cuando tú lo pidas, guardará una <b>huella de voz</b> por persona para reconocerla en las próximas reuniones.</p>
        <ul><li>Se descargan dos modelos (≈35 MB, una sola vez) a <code>%LOCALAPPDATA%\\AudiosApp\\models\\voces</code>.</li>
        <li>Las huellas van a <code>%LOCALAPPDATA%\\AudiosApp\\voces.json</code>, cifrado con tu cuenta de Windows, fuera de OneDrive, y se pueden olvidar en cualquier momento.</li>
        <li>Una huella de voz es un <b>dato biométrico</b>: solo se guarda si marcas que tienes el consentimiento de esa persona. Ese consentimiento es responsabilidad tuya.</li></ul>`, 'Activar');
      if (!ok) return;  // (la confirmación va encima de Ajustes: lo escrito sigue ahí, también al activar)
      try {
        const st = await api('/api/voces/activar', { on: true });
        S.config.voices = { enabled: true, models_ready: st.models.ready };
        e.target.checked = true;
        toast('🗣 Voces activadas' + (st.models.ready ? '' : ': descargando modelos…'));
        poll();
      }
      catch (err) { toast(esc(err.message), 'err'); }
    } else {
      try { await api('/api/voces/activar', { on: false }); S.config.voices = { enabled: false, models_ready: S.config.voices?.models_ready }; toast('Voces desactivadas (las personas guardadas se conservan hasta que las olvides)'); draw({ enabled: false }); }
      catch (err) { toast(esc(err.message), 'err'); }
    }
  };
  d.addEventListener('change', async e => {
    const n = e.target.closest('[data-vzname]');
    if (!n) return;
    // con destinos de salida reescribe los documentos de sus reuniones: si tarda, que se vea (fix-lento)
    n.disabled = true;
    const lento = setTimeout(() => toast('Guardando el nombre…'), 400);
    try { await api('/api/voces/persona', { id: n.dataset.vzname, nombre: n.value }); toast('Nombre guardado: las reuniones pasadas y futuras lo usan'); } catch (err) { toast(esc(err.message), 'err'); poll(); }
    finally { clearTimeout(lento); n.disabled = false; }
  });
  d.addEventListener('click', async e => {
    const me = e.target.closest('[data-vzme]'), fg = e.target.closest('[data-vzforget]'), all = e.target.closest('#vzForgetAll'), rt = e.target.closest('#vzRetry');
    try {
      if (me) { await api('/api/voces/persona', { id: me.dataset.vzme, es_usuario: true }); poll(); }
      else if (fg) { if (confirm('¿Olvidar esta persona? Se borran su nombre y sus huellas de voz de este PC.')) { await api('/api/voces/olvidar', { id: fg.dataset.vzforget }); poll(); } }
      else if (all) { if (confirm('¿Olvidar TODAS las personas y sus huellas de voz? No se puede deshacer.')) { await api('/api/voces/olvidar', { all: true }); poll(); } }
      else if (rt) { await api('/api/voces/activar', { on: true }); poll(); }
    } catch (err) { toast(esc(err.message), 'err'); }
  });
}
