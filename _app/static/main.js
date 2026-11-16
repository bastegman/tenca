/* Tenca: el punto de entrada de la interfaz. index.html carga solo este módulo; lo demás llega por import.

   Orden de arranque. Al cargarse, cada fichero registra sus escuchas (clics, teclas, cambios de dirección…) y prepara
   su parte del estado S de app.js, así que tiene que evaluarse después de app.js. Y el orden entre ellos también
   cuenta: cuando dos ficheros escuchan el mismo evento en el mismo nodo, responden en el orden en que se registraron.
   app.js importa de casi todos y casi todos de app.js (un ciclo), y en un ciclo de módulos lo que decide el orden es
   por dónde se entra. Por eso cada fichero importa primero el anterior de esta cadena, y aquí se importa el último:

     app.js → notas.js → voces.js → aprender.js → directo.js → reunion.js → hoy.js → perfil.js → modelos.js →
     configurar.js → grabar.js → recibir.js → ajustes.js → conservar.js → pistas.js

   Se evalúan en ese orden, el de los <script> de antes. Un fichero nuevo va al final de la cadena: importa primero
   pistas.js, y aquí se importa él en lugar de pistas.js. Fuera de la cadena: nombres-audio.js (no hace nada al
   cargarse; también lo usa la página del móvil, movil.js) y pistas-worklet.js (el AudioWorklet de pistas.js, que se
   carga con audioWorklet.addModule).
   tests/test_modulos.py comprueba la cadena, los import y que nada más se cuelga de window. */
import './pistas.js';  // tiene que ser el primer import: el último de la cadena trae a todos los demás, en orden
import { onWindowClosing } from './app.js';

// Lo único de la interfaz que va en window, y por qué. Lo demás se importa.
const GLOBALES = {
  // la ventana propia (window.py, con pywebview) la llama por su nombre al pulsar la X: «handled» si la página se
  // ocupa del aviso de «sigue en segundo plano»
  onWindowClosing,
};
Object.assign(window, GLOBALES);
