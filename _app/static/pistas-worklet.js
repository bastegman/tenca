/* Tenca · mezcla de las dos pistas al reproducir (AudioWorklet; lo carga pistas.js).
   Una grabación en dos pistas lleva el micro en el canal izquierdo y lo que suena en el PC en el derecho (pistas.py).
   Aquí se mezclan a mono en los dos oídos: el micro × «tú» y sin su eco, lo que suena × «los demás», y el limitador de
   la grabadora. Los mismos números y el mismo orden que pistas.mezclar_bloque (lo que hace «Exportar audio mezclado»):
   - cada canal se grabó con su limitador: se deshace antes de sumar (soft_unlimit);
   - eco: por ventanas de 20 ms, si lo más fuerte que ha sonado en el PC en los últimos 0,6 s pasa de -45 dB y el
     micro queda 6 dB por debajo de ello, es el eco de los altavoces y el micro se baja 18 dB (en ~10 ms; vuelve en
     ~20 ms). El audio sale con una ventana de retraso, así la ganancia de cada ventana se decide con ella entera,
     como al exportar;
   - en modo «normal» (cualquier otro audio) pasa tal cual. */
'use strict';
const KNEE = 0.8, ECO_DB = -18, ECO_SOBRE_DB = -45, ECO_MARGEN_DB = -6, VENTANA_S = 0.02, COLA_S = 0.6,
  ATAQUE_S = 0.01, SUELTA_S = 0.02;

function unlimit(y) {
  const a = Math.abs(y);
  if (a <= KNEE) return y;
  const z = Math.min((a - KNEE) / (1 - KNEE), 1 - 1e-6);
  return Math.sign(y) * (KNEE + (1 - KNEE) * Math.atanh(z));
}
function limit(x) {
  const a = Math.abs(x);
  const y = a > KNEE ? Math.sign(x) * (KNEE + (1 - KNEE) * Math.tanh((a - KNEE) / (1 - KNEE))) : x;
  return Math.max(-1, Math.min(1, y));
}

class PistasProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.modo = 'normal'; this.tu = 1; this.demas = 1; this.eco = true;
    this.w = Math.max(1, Math.round(VENTANA_S * sampleRate));
    this.k = Math.max(1, Math.round(COLA_S / VENTANA_S));
    this.ka = 1 - Math.exp(-VENTANA_S / ATAQUE_S); this.ks = 1 - Math.exp(-VENTANA_S / SUELTA_S);
    this.reset();
    this.port.onmessage = e => {
      const d = e.data || {};
      if (d.modo !== undefined && d.modo !== this.modo) { this.modo = d.modo; this.reset(); }
      for (const k of ['tu', 'demas']) if (typeof d[k] === 'number' && isFinite(d[k])) this[k] = Math.max(0, Math.min(2, d[k]));
      if (typeof d.eco === 'boolean') this.eco = d.eco;
      if (d.estado) {  // para los tests y el diagnóstico: niveles (RMS) de lo último que ha pasado
        const n = Math.max(1, this.medN), r = v => Math.sqrt(v / n);
        this.port.postMessage({ modo: this.modo, g: this.g, bajado: this.bajado, ventanas: this.ventanas,
          rms: { izq: r(this.medL), der: r(this.medR), sale: r(this.medO) } });
        this.medL = this.medR = this.medO = this.medN = 0;
      }
    };
  }
  reset() {
    // la ventana que se está llenando (entra) y la anterior, que es la que sale, con su ganancia ya decidida
    const w = this.w;
    this.inL = new Float32Array(w); this.inR = new Float32Array(w); this.i = 0; this.sl = 0; this.sr = 0;
    this.outL = new Float32Array(w); this.outR = new Float32Array(w);
    this.g = 1; this.gPrev = 1; this.gWin = 1; this.bajado = 0; this.ventanas = 0;
    this.medL = 0; this.medR = 0; this.medO = 0; this.medN = 0;
    this.dr = new Float64Array(this.k).fill(-120); this.j = 0;  // lo que sonó en las últimas k ventanas
  }
  decidir() {
    const dl = 10 * Math.log10(this.sl / this.w + 1e-12), dr = 10 * Math.log10(this.sr / this.w + 1e-12);
    this.dr[this.j] = dr; this.j = (this.j + 1) % this.k;
    let ref = -Infinity;
    for (let i = 0; i < this.k; i++) if (this.dr[i] > ref) ref = this.dr[i];
    const esEco = ref > ECO_SOBRE_DB && dl < ref + ECO_MARGEN_DB;
    const obj = esEco ? Math.pow(10, ECO_DB / 20) : 1;
    this.g += (obj - this.g) * (obj < this.g ? this.ka : this.ks);
    this.ventanas++;
    if (esEco) this.bajado++;
  }
  process(inputs, outputs) {
    const inp = inputs[0], out = outputs[0];
    if (!out || !out.length) return true;
    if (!inp || !inp.length) { for (const c of out) c.fill(0); return true; }
    try {
      if (this.modo !== 'doble' || inp.length < 2) {
        for (let c = 0; c < out.length; c++) out[c].set(inp[Math.min(c, inp.length - 1)]);
        const a = inp[0], b = inp[Math.min(1, inp.length - 1)];
        for (let k = 0; k < a.length; k++) { this.medL += a[k] * a[k]; this.medR += b[k] * b[k]; this.medO += a[k] * a[k]; }
        this.medN += a.length;
        return true;
      }
      const L = inp[0], R = inp[1], n = L.length, w = this.w;
      const o0 = out[0], o1 = out[1] || out[0];
      for (let k = 0; k < n; k++) {
        // sale la muestra de hace una ventana, con la ganancia de su ventana (interpolada desde la anterior)
        const gl = this.eco ? this.gPrev + (this.gWin - this.gPrev) * ((this.i + 1) / w) : 1;
        const y = limit(unlimit(this.outL[this.i]) * this.tu * gl + unlimit(this.outR[this.i]) * this.demas);
        o0[k] = y; o1[k] = y;
        const l = L[k], r = R[k];
        this.medL += l * l; this.medR += r * r; this.medO += y * y;
        this.inL[this.i] = l; this.inR[this.i] = r;
        this.sl += l * l; this.sr += r * r;
        if (++this.i === w) {  // ventana completa: se decide su ganancia y pasa a ser la que sale
          this.decidir();
          this.gPrev = this.gWin; this.gWin = this.g;
          const tl = this.outL, tr = this.outR;
          this.outL = this.inL; this.outR = this.inR; this.inL = tl; this.inR = tr;
          this.i = 0; this.sl = 0; this.sr = 0;
        }
      }
      this.medN += n;
    } catch (e) {  // nunca silencio: si algo falla, tal cual
      for (let c = 0; c < out.length; c++) out[c].set(inp[Math.min(c, inp.length - 1)]);
    }
    return true;
  }
}
registerProcessor('pistas', PistasProcessor);
