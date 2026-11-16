'use strict';
/* Qué pasa con el audio de cada reunión (punto 17 del review del 14/11/2026; en el servidor, conservar.py y
   app_conservar.py): «Guardar» (por defecto), «Guardar 15 días» o «Borrar al terminar». Se elige al grabar (la
   tarjeta) y se cambia aquí, en Detalles; la cabecera de la reunión dice qué pasará y cuándo. Borrar manda solo el
   audio a la papelera de Windows, y solo cuando la reunión ya tiene transcripción y resumen. Sin audio (ya en la
   papelera), la reunión sigue con sus textos y no se reproduce.
   Se carga después de app.js y notas.js: usa $, esc, I, S, main, toast, saveProps, jobFor, isLive, curAudio,
   renderAudioHead, refreshTree. app.js y notas.js lo llaman con `typeof … === 'function'`. */

const AUDIO_POL = [['guardar', 'Guardar'], ['15-dias', 'Guardar 15 días'], ['borrar', 'Borrar al terminar']];
const AUDIO_POL_AYUDA = {
  guardar: 'El audio se queda junto a la transcripción, como siempre.',
  '15-dias': 'A los 15 días de terminar la grabación, el audio va a la papelera de Windows. La transcripción, el resumen y las notas se quedan.',
  borrar: 'En cuanto la reunión tenga la transcripción y el resumen, el audio va a la papelera de Windows. La transcripción, el resumen y las notas se quedan.',
};
// por qué no se ha mandado aún (conservar.debe_borrar en el servidor: a.audio_espera; y lo que sabe la interfaz)
const AUDIO_ESPERA = {
  grabando: 'cuando termine de grabarse', sin_texto: 'cuando esté transcrita',
  a_medias: 'cuando la transcripción llegue hasta el final («Volver a transcribir» la completa)',
  huecos: 'cuando esté transcrita entera: al directo le faltan trozos («Volver a transcribir» la completa)',
  sin_resumen: 'cuando tenga resumen',
  voces: 'cuando se sepa quién habla en toda la reunión («Volver a identificar»)',
  ocupada: `cuando ${APP_NAME} termine con ella`, nube: 'cuando OneDrive la tenga en este PC (ahora está solo en línea)',
};
const AUDIO_NO = {
  sin_papelera: 'Esta carpeta no tiene papelera de Windows (una unidad de red o un USB, o la papelera borra al momento): el audio no se borra. Bórralo a mano si quieres.',
  grande: 'El audio no cabe en la papelera de Windows (Windows lo borraría para siempre): no se borra. Bórralo a mano si quieres.',
  en_uso: 'No se ha podido mandar el audio a la papelera: está abierto en otro programa (o en el reproductor). Se vuelve a intentar cada pocos minutos.',
};
const AUDIO_MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];

function audioFecha(iso) {  // «30 nov» (y el año si no es este)
  const d = new Date(iso || '');
  if (isNaN(d)) return '';
  return `${d.getDate()} ${AUDIO_MESES[d.getMonth()]}${d.getFullYear() !== new Date().getFullYear() ? ` de ${d.getFullYear()}` : ''}`;
}
function audioPol(a) { return (a && a.audio_pol) || 'guardar'; }
function audioEspera(a) {
  if (isLive(a) || a.recording) return 'grabando';
  if (jobFor(a.path)) return 'ocupada';
  return a.audio_espera || null;
}
/* qué pasará con el audio, en una frase (null si se guarda y no hay nada que decir) */
function audioDestinoTxt(a) {
  if (a.sin_audio) {
    const f = audioFecha(a.audio_borrado);
    return `El audio está en la papelera de Windows${f ? ` desde el ${f}` : ''}. La transcripción, el resumen y las notas siguen aquí.`;
  }
  const pol = audioPol(a);
  if (pol === 'guardar') return null;
  if (a.audio_no && AUDIO_NO[a.audio_no]) return AUDIO_NO[a.audio_no];
  const esp = audioEspera(a), hasta = a.audio_hasta ? new Date(a.audio_hasta) : null;
  if (pol === '15-dias' && hasta && hasta > new Date()) {
    const falta = { sin_texto: 'la transcripción', a_medias: 'la transcripción entera', huecos: 'la transcripción entera', sin_resumen: 'resumen', voces: 'quién habla en toda la reunión' }[esp];
    return `El audio irá a la papelera el ${audioFecha(a.audio_hasta)}, 15 días después de grabarla${falta ? ` (si para entonces ya tiene ${falta})` : ''}. Los textos se quedan.`;
  }
  return `El audio irá a la papelera ${(esp && AUDIO_ESPERA[esp]) || 'en unos minutos'}. Los textos se quedan.`;
}
function conservarLineaHtml(a) {
  const t = audioDestinoTxt(a);
  if (!t) return '';
  return `<div class="audio-destino${a.sin_audio ? ' sin' : ''}${a.audio_no ? ' no' : ''}" data-audio-destino>${I.trash}<span>${esc(t)}</span>${a.sin_audio ? '' : ' <button type="button" class="plink" data-act="audio-cambiar">Cambiar</button>'}</div>`;
}
/* la fila de Detalles */
function conservarPropHtml(a) {
  if (a.sin_audio) {
    const f = audioFecha(a.audio_borrado);
    return `<div class="prow"><span class="pk">${I.trash}Audio</span><span class="pv">En la papelera de Windows${f ? ` desde el ${esc(f)}` : ''}</span></div>`;
  }
  const pol = audioPol(a);
  return `<div class="prow"><span class="pk">${I.trash}Audio</span><span class="pv"><select class="pstate" data-prop="audio" aria-label="Qué pasa con el audio de esta reunión">${AUDIO_POL.map(([v, l]) => `<option value="${v}"${v === pol ? ' selected' : ''}>${l}</option>`).join('')}</select>
    <span class="muted audio-ayuda">${esc(AUDIO_POL_AYUDA[pol])}</span></span></div>`;
}
/* escuchar una reunión sin audio: se dice una vez y no se intenta */
function audioNoSeOye(a) {
  if (!a || !a.sin_audio) return false;
  toast(esc(audioDestinoTxt(a)) + ' Para oírlo, recupéralo de la papelera.', 'err');
  return true;
}

main.addEventListener('change', async e => {
  if (e.target.dataset?.prop !== 'audio') return;
  const a = curAudio();
  if (!a) return;
  const v = e.target.value, antes = audioPol(a);
  const r = await saveProps(a, { audio: v });
  if (!r) { e.target.value = antes; return; }
  a.audio_pol = (r.props || {}).audio || 'guardar';
  if (v === '15-dias' && a.mtime) a.audio_hasta = new Date((a.mtime + 15 * 86400) * 1000).toISOString();
  const ay = e.target.closest('.pv')?.querySelector('.audio-ayuda');
  if (ay) ay.textContent = AUDIO_POL_AYUDA[a.audio_pol];
  if (typeof renderAudioHead === 'function') renderAudioHead(a, true);
  toast(v === 'guardar' ? 'El audio de esta reunión se guarda' : esc(audioDestinoTxt(a) || ''));
  if (typeof refreshTree === 'function') refreshTree(true);
});
main.addEventListener('click', e => {
  const b = e.target.closest('[data-act="audio-cambiar"]');
  if (!b) return;
  e.preventDefault();
  const det = $('.pdet');
  if (det) det.open = true;
  const sel = $('select[data-prop="audio"]');
  if (sel) { sel.scrollIntoView({ block: 'center' }); sel.focus(); }
});
