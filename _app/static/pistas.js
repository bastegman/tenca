/* Tenca · reproductor de las grabaciones en dos pistas (punto 20 del review del 14/11/2026).
   La grabadora guarda tu micro en el canal izquierdo y lo que suena en el PC en el derecho (pistas.py). Sin esto se
   oirían tú en un oído y los demás en el otro. Al cargar un audio se mira su cabecera (una petición Range de 4 KB): si
   lleva la marca, el <audio> pasa por Web Audio y pistas-worklet.js lo mezcla en mono, con «Tú» y «Los demás» por
   separado y el eco de los altavoces quitado del micro (se puede desactivar: no cambia el archivo). Cualquier otro
   audio suena como siempre: Web Audio solo se monta la primera vez que se oye uno en dos pistas, y desde entonces los
   demás pasan por él tal cual.
   «Exportar audio mezclado» (POST /api/audio/mezclado) guarda en el Escritorio un WAV normal con esa mezcla. */
'use strict';
const Pistas = (() => {
  const MARCA = 'tenca:pistas=micro,sistema';
  const cache = new Map();          // ruta → Promise<bool>
  const cfg = Object.assign({ tu: 1, demas: 1, eco: true }, (() => {
    try { return JSON.parse(localStorage.getItem('pistas') || '{}'); } catch { return {}; }
  })());
  let ctx = null, node = null, montado = null, doble = false, ruta = null;

  function guardar() { localStorage.setItem('pistas', JSON.stringify(cfg)); }

  /* ¿Estos bytes (el principio de un WAV) son de una grabación en dos pistas? Como pistas.marca_en. */
  function marcaEn(b) {
    const s = (o, n) => String.fromCharCode(...b.subarray(o, o + n));
    const u32 = o => (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;
    const u16 = o => b[o] | (b[o + 1] << 8);
    if (b.length < 12 || s(0, 4) !== 'RIFF' || s(8, 4) !== 'WAVE') return false;
    let off = 12, fmt = false, marca = false;
    while (off + 8 <= b.length) {
      const id = s(off, 4), n = u32(off + 4);
      if (id === 'data') return fmt && marca;
      if (n === 0xFFFFFFFF) return false;
      const ini = off + 8;
      if (id === 'fmt ' && n >= 16) fmt = u16(ini) === 1 && u16(ini + 2) === 2 && u16(ini + 14) === 16;
      else if (id === 'LIST' && s(ini, 4) === 'INFO') marca = s(ini, Math.min(n, b.length - ini)).includes(MARCA);
      off = ini + n + (n & 1);
    }
    return false;
  }

  function esDoble(path) {
    if (!/\.wav$/i.test(path || '')) return Promise.resolve(false);
    if (!cache.has(path)) {
      cache.set(path, (async () => {
        try {
          const r = await fetch('/media?path=' + encodeURIComponent(path), { headers: { Range: 'bytes=0-4095' } });
          if (!r.ok) return false;
          return marcaEn(new Uint8Array(await r.arrayBuffer()));
        } catch { return false; }
      })().then(v => { if (!v) cache.delete(path); return v; }));  // un fallo de red no se queda para siempre
    }
    return cache.get(path);
  }

  /* Web Audio, la primera vez que hace falta (siempre tras un clic: el navegador ya deja sonar). Una vez conectado, el
     <audio> solo suena por aquí: el nodo deja pasar tal cual lo que no es de dos pistas. */
  function montar() {
    if (montado) return montado;
    montado = (async () => {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC || !AC.prototype || !('audioWorklet' in AC.prototype)) throw new Error('este navegador no tiene AudioWorklet');
      const c = new AC();
      await c.audioWorklet.addModule('/pistas-worklet.js');
      const n = new AudioWorkletNode(c, 'pistas', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
        channelCount: 2, channelCountMode: 'explicit', channelInterpretation: 'speakers' });
      c.createMediaElementSource(audio).connect(n);
      n.connect(c.destination);
      ctx = c; node = n;
      return true;
    })();
    montado.catch(e => { montado = null; toast(`No se pueden mezclar las dos pistas en esta ventana (${esc(e.message)}): se oirá tu micro en un oído y los demás en el otro. «Exportar audio mezclado» sí funciona.`, 'err'); });
    return montado;
  }

  function enviar() {
    if (node) node.port.postMessage({ modo: doble ? 'doble' : 'normal', tu: cfg.tu, demas: cfg.demas, eco: cfg.eco });
  }

  /* loadAudio (app.js) la llama al cambiar de audio. */
  async function cargar(path) {
    ruta = path;
    const v = await esDoble(path);
    if (ruta !== path) return;  // ya se ha cambiado a otro
    doble = v;
    $('#player').classList.toggle('pistas', v);
    if (!v) { cerrarPanel(); enviar(); return; }
    try { await montar(); } catch { return; }
    if (ruta !== path) return;
    enviar();
    if (ctx.state !== 'running') ctx.resume().catch(() => {});
  }

  /* ---------------------------------------------------------------- panel */
  function pct(v) { return Math.round(v * 100) + ' %'; }
  function panel() {
    let p = $('#pistasPanel');
    if (p) return p;
    p = document.createElement('div');
    p.id = 'pistasPanel';
    p.className = 'pistas-panel hidden';
    p.innerHTML = `
      <div class="pp-tit">Dos pistas</div>
      <p class="pp-txt">Se grabó tu micro aparte del audio del PC. Ajusta cada uno al escucharlo: el archivo no cambia.</p>
      <label class="pp-fila">Tú <input type="range" id="ppTu" min="0" max="2" step="0.05"><span id="ppTuV"></span></label>
      <label class="pp-fila">Los demás <input type="range" id="ppDemas" min="0" max="2" step="0.05"><span id="ppDemasV"></span></label>
      <label class="pp-check"><input type="checkbox" id="ppEco"> Quitar el eco de los altavoces de tu micro</label>
      <button type="button" class="btn" id="ppExportar">Exportar audio mezclado</button>
      <div class="pp-nota" id="ppNota"></div>`;
    document.body.appendChild(p);
    const tu = $('#ppTu', p), dem = $('#ppDemas', p), eco = $('#ppEco', p);
    const pintar = () => {
      tu.value = cfg.tu; dem.value = cfg.demas; eco.checked = cfg.eco;
      $('#ppTuV', p).textContent = pct(cfg.tu); $('#ppDemasV', p).textContent = pct(cfg.demas);
      for (const r of [tu, dem]) r.style.setProperty('--pct', (100 * r.value / 2) + '%');
    };
    tu.addEventListener('input', () => { cfg.tu = +tu.value; pintar(); guardar(); enviar(); });
    dem.addEventListener('input', () => { cfg.demas = +dem.value; pintar(); guardar(); enviar(); });
    eco.addEventListener('change', () => { cfg.eco = eco.checked; guardar(); enviar(); });
    $('#ppExportar', p).addEventListener('click', exportar);
    p.pintar = pintar;
    pintar();
    return p;
  }
  function abrirPanel() {
    const p = panel();
    p.pintar();
    $('#ppNota', p).textContent = '';
    p.classList.remove('hidden');
    $('#pPistas').setAttribute('aria-expanded', 'true');
  }
  function cerrarPanel() {
    const p = $('#pistasPanel');
    if (p) p.classList.add('hidden');
    const b = $('#pPistas');
    if (b) b.setAttribute('aria-expanded', 'false');
  }
  async function exportar() {
    const b = $('#ppExportar'), nota = $('#ppNota');
    if (!ruta || !doble) return;
    b.disabled = true;
    nota.textContent = 'Mezclando… (unos segundos por hora de grabación)';
    try {
      const r = await api('/api/audio/mezclado', { path: ruta, tu: cfg.tu, demas: cfg.demas, eco: cfg.eco }, 600000);
      nota.textContent = '';
      toast(`Guardado en el Escritorio: <b>${esc(r.nombre)}</b>. Es un WAV normal, para abrirlo con otro programa.`);
    } catch (e) {
      nota.textContent = '';
      toast(esc(e.message), 'err');
    } finally { b.disabled = false; }
  }

  const btn = $('#pPistas');
  if (btn) btn.addEventListener('click', e => {
    e.stopPropagation();
    const p = $('#pistasPanel');
    p && !p.classList.contains('hidden') ? cerrarPanel() : abrirPanel();
  });
  document.addEventListener('click', e => {
    const p = $('#pistasPanel');
    if (p && !p.classList.contains('hidden') && !p.contains(e.target)) cerrarPanel();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') cerrarPanel(); });
  audio.addEventListener('play', () => { if (ctx && ctx.state !== 'running') ctx.resume().catch(() => {}); });

  /* para los tests: el estado del nodo (modo, ganancia del micro, ventanas con eco) */
  function estado() {
    return new Promise(res => {
      if (!node) return res({ montado: false, doble });
      const t = setTimeout(() => res({ montado: true, doble, ctx: ctx.state }), 1000);
      node.port.onmessage = e => { clearTimeout(t); res(Object.assign({ montado: true, doble, ctx: ctx.state }, e.data)); };
      node.port.postMessage({ estado: true });
    });
  }
  return { cargar, esDoble, marcaEn, estado, cfg };
})();
