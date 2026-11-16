# -*- coding: utf-8 -*-
"""La interfaz son módulos ES (punto 52 del review del 14/11/2026): un solo punto de entrada en index.html, lo que un
fichero usa de otro llega por import, el orden de arranque es una cadena explícita y en window solo queda la lista de
main.js. Y, por el punto 50, sin notas internas en los comentarios de static/.

Hasta el 16/11/2026 eran 16 <script> uno tras otro que se veían por nombres globales y por el orden de carga
(`typeof X === 'function'`, `window.X?.()`): un fichero que faltara o se moviera dejaba la interfaz a medias sin un
error. Todo esto se mira sobre el texto (con un lector de JS mínimo: comentarios, cadenas, plantillas y expresiones
regulares fuera); que se analicen como módulo lo mira node, si está; que carguen en un navegador, test_ui_smoke."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from support import STATIC

NO_ESCRITORIO = ("main.js", "movil.js", "pistas-worklet.js")
SUELTOS = ("nombres-audio.js",)  # en la página de escritorio, pero fuera de la cadena (no hace nada al cargarse)
# lo único de la interfaz que va en window (main.js), y quién lo usa
GLOBALES = {"onWindowClosing": "window.py (pywebview) la llama por su nombre al cerrar la ventana"}
PALABRAS = frozenset(
    "await break case catch class const continue debugger default delete do else export extends false finally for "
    "function if import in instanceof let new null of return static super switch this throw true try typeof var void "
    "while with yield async get set from as".split())


def leer(nombre):
    with open(os.path.join(STATIC, nombre), encoding="utf-8") as f:
        return f.read()


def ficheros_js():
    return sorted(n for n in os.listdir(STATIC) if n.endswith(".js"))


def fichas(src):
    """Las piezas de código de un fichero JS: ("id", nombre, pos) y ("p", signo, pos), sin comentarios, cadenas, el
    texto de las plantillas ni las expresiones regulares (sí lo que va dentro de ${…})."""
    out, i, n = [], 0, len(src)
    pila = []  # una entrada por cada ${ abierto: las llaves abiertas dentro
    prev = None  # la última pieza significativa (para distinguir / de división y de expresión regular)

    def regex_aqui():
        if prev is None:
            return True
        k, v = prev[0], prev[1]
        if k == "id":
            return v in PALABRAS and v not in ("this", "super", "null", "true", "false")
        if k == "num":
            return False
        return v not in (")", "]", "}")

    def plantilla(j):
        # j: justo después de la comilla invertida (o del } que cierra un ${…}); devuelve dónde sigue el código
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1, False
            if c == "$" and j + 1 < n and src[j + 1] == "{":
                return j + 2, True
            j += 1
        return n, False

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            prev = ("str", "", i)
        elif c == "`":
            i, abre = plantilla(i + 1)
            if abre:
                pila.append(0)
            prev = ("str", "", i)
        elif c == "/" and regex_aqui():
            j, clase = i + 1, False
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "[":
                    clase = True
                elif d == "]":
                    clase = False
                elif d == "/" and not clase:
                    break
                elif d == "\n":
                    break
                j += 1
            j += 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            i = j
            prev = ("str", "", i)
        elif c.isalpha() or c in "_$" or ord(c) > 127:
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_$" or ord(src[j]) > 127):
                j += 1
            prev = ("id", src[i:j], i)
            out.append(prev)
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            prev = ("num", src[i:j], i)
            i = j
        else:
            if c == "{" and pila:
                pila[-1] += 1
            elif c == "}" and pila:
                if pila[-1] == 0:
                    pila.pop()
                    i, abre = plantilla(i + 1)
                    if abre:
                        pila.append(0)
                    prev = ("str", "", i)
                    continue
                pila[-1] -= 1
            sig = src[i:i + 3] if src[i:i + 3] in ("...", "?.(", "?.[") else src[i:i + 2] if src[i:i + 2] in (
                "?.", "=>", "==", "!=", "&&", "||", "??", "<=", ">=", "++", "--", "+=", "-=") else c
            if sig in ("?.(", "?.["):
                sig = "?."
            prev = ("p", sig, i)
            out.append(prev)
            i += len(sig)
    return out


def sentencias_import(src):
    """[(desde, [nombres], pos)] de los import del fichero, en orden; `import './x.js'` da nombres vacíos."""
    out = []
    for m in re.finditer(r"^import\s+(?:\{([^}]*)\}\s+from\s+)?'([^']+)';", src, re.M):
        nombres = [x.strip() for x in (m.group(1) or "").split(",") if x.strip()]
        out.append((m.group(2), nombres, m.start()))
    return out


def exportados(src):
    m = re.findall(r"^export \{([^}]*)\};", src, re.M)
    return {x.strip() for bloque in m for x in bloque.split(",") if x.strip()}


def de_primer_nivel(src):
    """Los nombres que un fichero declara en su primer nivel (en columna 0, como está escrito todo static/)."""
    nombres = set()
    for m in re.finditer(r"^(?:async\s+)?function\*?\s+([\w$]+)|^class\s+([\w$]+)", src, re.M):
        nombres.add(m.group(1) or m.group(2))
    for m in re.finditer(r"^(?:const|let|var)\s", src, re.M):
        # los declaradores separados por comas al nivel 0: `let a = 1, b = [2, 3];`
        hondo, esperando, k = 0, True, 0
        resto = fichas(src[m.end():m.end() + 4000])
        while k < len(resto):
            kind, v, _ = resto[k]
            if esperando and kind == "id":
                nombres.add(v)
                esperando = False
            elif kind == "p" and v in "([{":
                hondo += 1
            elif kind == "p" and v in ")]}":
                hondo -= 1
            elif kind == "p" and v == "," and hondo == 0:
                esperando = True
            elif kind == "p" and v == ";" and hondo == 0:
                break
            k += 1
    return nombres


def declarados(src):
    """Todo nombre que el fichero declara en cualquier sitio (también parámetros y variables de dentro): si un nombre
    de otro fichero se declara aquí, puede ser otra variable con el mismo nombre y no se da por usado de fuera."""
    fs = fichas(src)
    out = set()
    for k, (kind, v, _) in enumerate(fs):
        if kind != "id":
            continue
        ant = fs[k - 1] if k else ("p", "", 0)
        sig = fs[k + 1] if k + 1 < len(fs) else ("p", "", 0)
        if ant[0] == "id" and ant[1] in ("function", "class", "const", "let", "var"):
            out.add(v)
        elif sig[1] == "=>" and ant[1] != ".":
            out.add(v)
    # listas de parámetros y patrones: lo que va entre ( ) delante de => o detrás de function, y { } / [ ] de un
    # const/let/var
    for k, (kind, v, _) in enumerate(fs):
        abre = None
        if kind == "p" and v == "(":
            ant = fs[k - 1] if k else ("p", "", 0)
            catch = ant[1] == "catch" and k > 1 and fs[k - 2][1] == "}"  # try { } catch (e): no .catch(…)
            if ant[0] == "id" and (ant[1] == "function" or (k > 1 and fs[k - 2][1] == "function")) or catch:
                abre = ")"
            else:  # ¿una flecha? (a, b) =>
                hondo, j = 0, k
                while j < len(fs):
                    if fs[j][1] == "(":
                        hondo += 1
                    elif fs[j][1] == ")":
                        hondo -= 1
                        if hondo == 0:
                            break
                    j += 1
                if j + 1 < len(fs) and fs[j + 1][1] == "=>":
                    abre = ")"
        elif kind == "p" and v in "{[" and k and fs[k - 1][1] in ("const", "let", "var"):
            abre = "}" if v == "{" else "]"
        if abre:
            hondo, j = 0, k
            while j < len(fs):
                kv = fs[j][1]
                if kv in "([{":
                    hondo += 1
                elif kv in ")]}":
                    hondo -= 1
                    if hondo == 0:
                        break
                elif fs[j][0] == "id" and fs[j + 1][1] != ":" and fs[j - 1][1] not in (".", "="):
                    out.add(kv)  # (lo de detrás de un = es un valor por defecto: se lee, no se declara)
                j += 1
    return out


def usados(src):
    """Los nombres que el código lee o llama, sin los de detrás de un punto ni las claves de un objeto."""
    fs = fichas(src)
    out = set()
    for k, (kind, v, _) in enumerate(fs):
        if kind != "id" or v in PALABRAS:
            continue
        ant = fs[k - 1] if k else ("p", "", 0)
        sig = fs[k + 1] if k + 1 < len(fs) else ("p", "", 0)
        if ant[1] in (".", "?."):
            continue
        if sig[1] == ":" and ant[1] in ("{", ","):
            continue  # clave de objeto
        out.add(v)
    return out


def sin_import_export(src):
    src = re.sub(r"^import\b[^;]*;", "", src, flags=re.M)
    return re.sub(r"^export \{[^}]*\};", "", src, flags=re.M)


def cadena():
    """El orden de arranque que documenta main.js: «app.js → notas.js → … → pistas.js»."""
    m = re.search(r"((?:[\w-]+\.js\s*→\s*)+[\w-]+\.js)", leer("main.js"))
    return re.findall(r"[\w-]+\.js", m.group(1)) if m else []


class ModulosTests(unittest.TestCase):
    def test_index_carga_un_solo_modulo(self):
        html = leer("index.html")
        self.assertEqual(re.findall(r"<script\b[^>]*>", html), ['<script type="module" src="/main.js">'])

    def test_todos_son_modulos_con_import_y_export(self):
        for nombre in ficheros_js():
            if nombre in ("main.js", "pistas-worklet.js"):
                continue
            with self.subTest(nombre):
                src = leer(nombre)
                self.assertNotIn("'use strict'", src)  # un módulo ya lo es; antes, 5 ficheros no lo eran
                if nombre != "nombres-audio.js":
                    self.assertTrue(sentencias_import(src), "sin import")
                if nombre != "movil.js":
                    self.assertTrue(exportados(src), "sin export")
                # solo las dos formas que leen estos tests: `import { a, b } from './x.js';` e `import './x.js';`, y
                # un único `export { … };` (otra forma no la vería ni la cadena ni la comprobación de los import)
                lineas = re.findall(r"^import\b.*", src, re.M)
                self.assertEqual(len(lineas), len(sentencias_import(src)), lineas)
                self.assertEqual(re.findall(r"^export\b(?! \{)", src, re.M), [])
                self.assertLessEqual(len(re.findall(r"^export \{", src, re.M)), 1)

    def test_cadena_de_arranque(self):
        orden = cadena()
        escritorio = [n for n in ficheros_js() if n not in NO_ESCRITORIO and n not in SUELTOS]
        self.assertEqual(sorted(orden), sorted(escritorio), "cada fichero de la página de escritorio, en la cadena")
        self.assertEqual(orden[0], "app.js")
        imp = sentencias_import(leer("main.js"))
        self.assertEqual(imp[0][0], "./" + orden[-1], "main.js importa primero el último de la cadena")
        for anterior, nombre in zip(orden, orden[1:]):
            with self.subTest(nombre):
                primero = sentencias_import(leer(nombre))[0]
                self.assertEqual(primero[0], "./" + anterior, "su primer import es el anterior de la cadena")
        for nombre in SUELTOS:  # fuera de la cadena: no importa nada (la página del móvil lo carga sin la app)
            self.assertEqual(sentencias_import(leer(nombre)), [])

    def test_lo_que_se_importa_existe(self):
        # un import de un nombre que el otro no exporta (o un export de algo que no existe) es un error al enlazar:
        # no arranca ningún módulo y la ventana queda vacía
        for nombre in ficheros_js():
            src = leer(nombre)
            with self.subTest(nombre):
                propios = de_primer_nivel(src)
                for x in exportados(src):
                    self.assertIn(x, propios, f"exporta {x}, que no declara")
                for desde, nombres, _ in sentencias_import(src):
                    self.assertTrue(desde.startswith("./") and desde.endswith(".js"), desde)
                    otro = exportados(leer(desde[2:]))
                    for x in nombres:
                        self.assertIn(x, otro, f"importa {x} de {desde}, que no lo exporta")

    def test_lo_de_otro_fichero_llega_por_import(self):
        # el fallo que se ve solo al pulsar: un nombre de otro fichero usado sin importarlo es un ReferenceError
        # en ese momento, no al cargar
        tops = {n: de_primer_nivel(leer(n)) for n in ficheros_js()}
        duenos = {}
        for n, ns in tops.items():
            for x in ns:
                self.assertNotIn(x, duenos, f"{x} se declara en {duenos.get(x)} y en {n}")
                duenos[x] = n
        for nombre in ficheros_js():
            src = leer(nombre)
            importados = {x for _, ns, _ in sentencias_import(src) for x in ns}
            cuerpo = sin_import_export(src)
            ajenos = {x for x, n in duenos.items() if n != nombre}
            with self.subTest(nombre):
                if nombre in NO_ESCRITORIO:
                    # otra página (el móvil) u otro ámbito (el worklet): sus $ y esc son suyos
                    locales = declarados(cuerpo) | tops[nombre]
                else:
                    # en la página de escritorio, ningún nombre de dentro (parámetro o variable) se llama como uno
                    # de primer nivel de otro fichero: así, todo lo que se usa con ese nombre es lo de fuera y tiene
                    # que llegar por import (si no, una sombra local escondería un import que falta; y en los tests
                    # de navegador support.EXPONER_MODULOS lo resolvería por window)
                    sombras = sorted((declarados(cuerpo) - tops[nombre]) & ajenos)
                    self.assertEqual(sombras, [], "se llaman como algo de otro fichero")
                    locales = tops[nombre]
                faltan = sorted(f"{x} ({duenos[x]})" for x in usados(cuerpo)
                                if x in ajenos and x not in importados and x not in locales)
                self.assertEqual(faltan, [])

    def test_nada_mas_en_window(self):
        tops = set().union(*(de_primer_nivel(leer(n)) for n in ficheros_js()))
        for nombre in ficheros_js():
            src = sin_import_export(leer(nombre))
            with self.subTest(nombre):
                fs = fichas(src)
                for k, (kind, v, _) in enumerate(fs[:-2]):
                    if kind == "id" and v in ("window", "globalThis", "self") and fs[k + 1][1] in (".", "?."):
                        prop = fs[k + 2][1]
                        # window.X de un nombre de la interfaz: el «window.reunionFieldsetHtml?.()» de antes, que sin
                        # error deja de existir con módulos
                        self.assertNotIn(prop, tops, f"{v}.{prop}")
                        es_escritura = k + 3 < len(fs) and fs[k + 3][1] == "="  # («==» es otra pieza)
                        if es_escritura and nombre != "main.js":
                            self.fail(f"{nombre}: escribe {v}.{prop} (lo que va en window, en main.js)")
                    if kind == "id" and v == "typeof" and fs[k + 1][0] == "id" and fs[k + 1][1] in tops:
                        # «typeof X === 'function'»: con módulos X se importa y la guarda sobra
                        if fs[k + 1][1] not in de_primer_nivel(src):
                            self.fail(f"{nombre}: typeof {fs[k + 1][1]}")
                self.assertNotIn("Object.assign(window", src if nombre != "main.js" else "")
        main = leer("main.js")
        m = re.search(r"const GLOBALES = \{(.*?)\n\};", main, re.S)
        self.assertTrue(m)
        claves = re.findall(r"^\s*([\w$]+),\s*$", re.sub(r"//[^\n]*", "", m.group(1)), re.M)
        self.assertEqual(claves, list(GLOBALES))
        self.assertIn("Object.assign(window, GLOBALES);", main)

    def test_sin_notas_internas(self):
        # punto 50: el repo es público; las notas de trabajo (mensajes, nombres, «el portátil de…») no van en el
        # código. El enlace de donación lo pinta app.js desde la configuración: no hay correos en static/
        malas = re.compile(r"msgs? ?[0-9]{3,4}|\bmensaje [0-9]{3,4}|bernard|stegmaier|bastegman|port[aá]til de|"
                           r"\b(steve|claudia|linus|harvey)\b|/Users/|[A-Za-z]:\\\\Users\\\\|@[\w-]+\.(com|io|es)\b",
                           re.I)
        for nombre in sorted(os.listdir(STATIC)):
            if not nombre.endswith((".js", ".css", ".html")):
                continue
            with self.subTest(nombre):
                hallado = [f"{i}: {ln.strip()[:120]}" for i, ln in enumerate(leer(nombre).splitlines(), 1)
                           if malas.search(ln)]
                self.assertEqual(hallado, [])

    @unittest.skipUnless(shutil.which("node"), "node no instalado")
    def test_node_los_lee_como_modulo(self):
        # node --check con la extensión .mjs: lo analiza como módulo (modo estricto, import/export), sin ejecutarlo
        d = tempfile.mkdtemp(prefix="tenca-mjs-")
        self.addCleanup(shutil.rmtree, d, True)
        for nombre in ficheros_js():
            with self.subTest(nombre):
                p = os.path.join(d, nombre[:-3] + ".mjs")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(leer(nombre))
                r = subprocess.run(["node", "--check", p], capture_output=True, text=True, encoding="utf-8",
                                   timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)


class LectorTests(unittest.TestCase):
    """El lector mínimo de JS de arriba, contra lo que podría confundirle."""

    def test_cadenas_plantillas_y_regex_fuera(self):
        src = ("const a = 'toast(1)', b = `x ${esc(y)} ${'{'} z`, c = /api\\(/g, d = q / 2 / r;\n"
               "// toast() en un comentario\n/* api() */ f({ clave: 1, otra }); o.prop; o?.prop2;")
        self.assertEqual(usados(src), {"a", "b", "esc", "y", "c", "d", "q", "r", "f", "otra", "o"})
        self.assertEqual(de_primer_nivel(src), {"a", "b", "c", "d"})

    def test_declarados(self):
        src = ("function f(a, { b, c: d }) { const [e, g] = h; return x => (y, z) => a + e; } try { } catch (err) { }\n"
               "p.catch(e2 => avisa(e2)); q.then((r1) => pinta(r1)); function k(m = defecto) { }")
        self.assertTrue({"f", "a", "b", "d", "e", "g", "x", "y", "z", "err"} <= declarados(src))
        self.assertTrue({"e2", "r1"} <= declarados(src))
        for x in ("h", "avisa", "pinta", "defecto"):
            self.assertNotIn(x, declarados(src))
        self.assertNotIn("c", declarados(src))


if __name__ == "__main__":
    unittest.main()
