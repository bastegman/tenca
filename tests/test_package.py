# -*- coding: utf-8 -*-
"""Paquete instalable (build_package), plantillas de _app/export y coherencia de requirements/instalador."""
import importlib.util
import io
import os
import re
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from support import REPO, server

VERSION_RE = rb"\d{4}-\d{2}-\d{2}(?: \xc2\xb7 [0-9a-f]{7,12})?"  # «2026-10-31 · abc1234» (sin git, solo la fecha)


def load_build_zip():
    spec = importlib.util.spec_from_file_location("build_zip", os.path.join(REPO, "scripts", "build_zip.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="chuleta-pkg-")
        cls.dest = os.path.join(cls.tmp, "Tenca - instalador.zip")
        cls.n = server.build_package(cls.dest)
        cls.zip = zipfile.ZipFile(cls.dest)
        cls.names = set(cls.zip.namelist())

    @classmethod
    def tearDownClass(cls):
        cls.zip.close()

    def test_layout(self):
        for n in ("Tenca/INSTALAR.bat", "Tenca/ABRIR TENCA.bat", "Tenca/LEEME.txt", "Tenca/LICENSE",
                  "Tenca/_app/server.py", "Tenca/_app/engines.py", "Tenca/_app/worker.py", "Tenca/_app/live.py",
                  "Tenca/_app/recorder.py", "Tenca/_app/Tenca.bat", "Tenca/_app/instalar.ps1",
                  "Tenca/_app/version.txt", "Tenca/_app/static/index.html", "Tenca/_app/static/app.js",
                  "Tenca/_app/static/notas.js", "Tenca/_app/static/app.css", "Tenca/_app/static/tenca.ico",
                  "Tenca/_app/static/tenca.png", "Tenca/_app/calendario.py", "Tenca/_app/aviso.py",
                  "Tenca/_app/static/reunion.js", "Tenca/_app/export/ABRIR TENCA.bat"):
            self.assertIn(n, self.names)
        self.assertEqual(self.n, len(self.names))
        self.assertTrue(all(n.startswith("Tenca/") for n in self.names))
        self.assertFalse(any("Notas" in n for n in self.names))  # nada del nombre anterior (notas.js es la función)
        self.assertFalse(any("Vocabulario" in n or n.endswith((".wav", ".pyc")) or "__pycache__" in n
                             for n in self.names))
        self.assertFalse(any("tests/" in n or n.endswith("README.md") for n in self.names))
        self.assertIn("Tenca/LICENSE", self.names)  # MIT: el aviso acompaña a cada copia

    def test_crlf_bom_y_version(self):
        leeme = self.zip.read("Tenca/LEEME.txt")
        self.assertTrue(leeme.startswith("﻿".encode("utf-8")))
        self.assertIn(b"\r\n", leeme)
        self.assertNotIn(b"{FECHA}", leeme)
        version = self.zip.read("Tenca/_app/version.txt")
        self.assertFalse(version.startswith("﻿".encode("utf-8")))  # sin BOM: PowerShell 5 lo lee con -Encoding UTF8
        self.assertRegex(version, rb"^" + VERSION_RE + rb"\r\n$")
        self.assertIn(version.strip(), leeme.split(b"\r\n")[0])  # la 1.ª línea del LEEME lleva la misma versión
        ps1 = self.zip.read("Tenca/_app/instalar.ps1")
        self.assertTrue(ps1.startswith("﻿".encode("utf-8")))
        self.assertNotIn(b"\n\n\r", ps1)
        self.assertFalse(re.search(rb"(?<!\r)\n", ps1))  # todo CRLF
        bat = self.zip.read("Tenca/INSTALAR.bat")
        self.assertFalse(bat.startswith("﻿".encode("utf-8")))  # cmd.exe no quiere BOM
        self.assertFalse(re.search(rb"(?<!\r)\n", bat))
        py = self.zip.read("Tenca/_app/server.py")
        self.assertNotIn(b"\r\n", py)  # el código va tal cual

    def test_version_explicita(self):
        """build_zip.py pasa la versión calculada del commit: va a _app/version.txt y al {FECHA} del LEEME."""
        dest = os.path.join(self.tmp, "con-version.zip")
        n = server.build_package(dest, version="2026-01-02 · abc1234")
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(n, len(z.namelist()))
            self.assertEqual(z.read("Tenca/_app/version.txt"), "2026-01-02 · abc1234\r\n".encode("utf-8"))
            primera = z.read("Tenca/LEEME.txt").split(b"\r\n")[0].decode("utf-8-sig")
            self.assertIn("(versión 2026-01-02 · abc1234)", primera)
            self.assertIn(b"{FECHA}", z.read("Tenca/_app/export/LEEME.txt"))  # la plantilla sigue con el marcador

    def test_version_por_defecto(self):
        """Sin versión explícita: la de _app/version.txt de esta copia (una copia instalada que vuelve a exportar) o,
        si no la tiene, la fecha de hoy."""
        with mock.patch.object(server, "version_file", return_value=None):
            self.assertEqual(server.package_version(), time.strftime("%Y-%m-%d"))
        with mock.patch.object(server, "version_file", return_value="2026-03-04 · 0123abc"):
            self.assertEqual(server.package_version(), "2026-03-04 · 0123abc")

    def test_extra(self):
        dest = os.path.join(self.tmp, "con-extra.zip")
        extra = os.path.join(self.tmp, "Legal.txt")
        with open(extra, "w", encoding="utf-8") as f:
            f.write("a, b\n")
        server.build_package(dest, [(extra, "Tenca/Alfa/Vocabulario/Legal.txt")])
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(z.read("Tenca/Alfa/Vocabulario/Legal.txt"), "﻿a, b\r\n".encode("utf-8"))


class TemplatesTests(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(REPO, *parts), "rb") as f:
            return f.read()

    def test_raiz_y_export_coinciden(self):
        for name in ("INSTALAR.bat", "ABRIR TENCA.bat"):
            self.assertEqual(self.read(name), self.read("_app", "export", name), name)
        root, tpl = self.read("LEEME.txt"), self.read("_app", "export", "LEEME.txt")
        m = re.search(rb"\(versi\xc3\xb3n (" + VERSION_RE + rb")\)", root.split(b"\r\n")[0])
        self.assertTrue(m, "la 1.ª línea de LEEME.txt lleva «(versión AAAA-MM-DD)» o «(versión AAAA-MM-DD · sha)»")
        self.assertEqual(tpl.replace(b"{FECHA}", m.group(1)), root)
        self.assertEqual(tpl.count(b"{FECHA}"), 1)
        self.assertIn(b"(versi\xc3\xb3n {FECHA})", tpl.split(b"\r\n")[0])
        for n in ("ABRIR NOTAS.bat", os.path.join("_app", "Notas.bat"),
                  os.path.join("_app", "export", "ABRIR NOTAS.bat"),
                  os.path.join("_app", "static", "notas.ico"), os.path.join("_app", "static", "notas.png")):
            self.assertFalse(os.path.exists(os.path.join(REPO, n)), n)  # renombrados con git mv

    def test_leeme_de_una_pantalla(self):
        """El LEEME cabe en una pantalla (punto 45 del review): instalar, abrir, grabar, dónde queda todo y si algo
        falla; el detalle está en el README («Manual de uso»)."""
        leeme = self.read("_app", "export", "LEEME.txt").decode("utf-8-sig")
        self.assertLessEqual(len(leeme.split("\r\n")), 50)
        cabeceras = [ln.split(" (")[0] for ln in leeme.split("\r\n")[2:] if ln[:1].isalpha()]
        self.assertEqual([c for c in cabeceras if c == c.upper()],
                         ["INSTALAR", "ABRIR", "GRABAR", "DÓNDE QUEDA TODO", "SI ALGO FALLA"])
        self.assertIn("https://github.com/bastegman/tenca#readme", leeme)
        self.assertIn("## Manual de uso", self.read("README.md").decode("utf-8"))

    def test_requirements_igual_que_el_instalador(self):
        ps1 = self.read("_app", "instalar.ps1").decode("utf-8-sig")
        pkgs = []
        for var in ("packages", "optional"):  # el core y la librería opcional de voces se instalan por separado
            m = re.search(r'\$' + var + r' = @\(([^)]*)\)', ps1)
            pkgs += re.findall(r'"([^"]+)"', m.group(1))
        pkgs.sort()
        req = sorted(l.strip() for l in self.read("requirements.txt").decode("utf-8").splitlines()
                     if l.strip() and not l.startswith("#"))
        self.assertEqual(pkgs, req)

    def test_sin_rastro_de_clientes(self):
        """La app es agnóstica: nada de clientes, empresas ni personas en ningún fichero del repo (código, tests,
        scripts, workflows, CHANGELOG, README y LEEME)."""
        # La lista es privada (los clientes de cada usuario) y nunca se escribe en el repo: expresiones regulares
        # separadas por comas (o una por línea) en la variable de entorno NOTAS_PALABRAS_VETADAS, sin distinguir
        # mayúsculas y con ^ y $ en cada línea. Sin ella, el test se salta, salvo con NOTAS_EXIGIR_VETADAS=1, que lo
        # hace fallar (así una ruta mal escrita no lo deja saltado entre los demás). En la CI, tests.yml pone la lista
        # solo en el paso de este test, desde el secreto de Actions del mismo nombre, y no la exige: sin el secreto, el
        # test se salta y, con NOTAS_AVISAR_SIN_VETADAS, deja él mismo el ::warning del resumen del run
        # (AVISO_SIN_VETADAS), y el paso sale en verde. En local, con la lista en un fichero fuera del repo (escrita en
        # la orden quedaría en el historial):
        #   macOS/Linux:
        #     NOTAS_EXIGIR_VETADAS=1 NOTAS_PALABRAS_VETADAS="$(cat ~/tenca-vetadas.txt)" .venv/bin/python tests/run.py
        #   Windows (PowerShell, desde la raíz del repo):
        #     $env:NOTAS_EXIGIR_VETADAS = "1"
        #     $env:NOTAS_PALABRAS_VETADAS = Get-Content -Raw "$HOME\tenca-vetadas.txt"; python tests\run.py
        # Revisa lo que git tiene en el índice (un fichero nuevo cuenta desde su `git add`); sin git, la raíz y _app,
        # tests, scripts, .github y design. Al fallar da fichero, línea y número de patrón, nunca el texto encontrado:
        # el log de la CI de un repo público es público.
        patrones = palabras_vetadas()
        if not patrones:
            if vetadas_exigidas():
                self.fail("NOTAS_PALABRAS_VETADAS falta o está vacía y NOTAS_EXIGIR_VETADAS la exige: revisa la ruta "
                          "del fichero con la lista (en la CI, el secreto de Actions del mismo nombre); sin ella no se "
                          "revisa nada")
            if avisar_sin_vetadas():  # el paso de tests.yml: que se vea en el resumen del run, no solo en su log
                print(AVISO_SIN_VETADAS, flush=True)
            self.skipTest("NOTAS_PALABRAS_VETADAS sin definir")
        hits = buscar_palabras(patrones, ficheros_del_repo())
        if hits:
            lineas = [f"  {rel}:{n} (patrón {k})" for rel, n, k in hits[:50]]
            if len(hits) > 50:
                lineas.append(f"  … y {len(hits) - 50} más")
            cuantas = "1 coincidencia" if len(hits) == 1 else f"{len(hits)} coincidencias"
            self.fail(f"{cuantas} con NOTAS_PALABRAS_VETADAS (sin el texto):\n" + "\n".join(lineas))

    def test_agnosticismo_mira_todo_el_repo_y_calla_lo_que_encuentra(self):
        """El test de agnosticismo revisa también CHANGELOG, tests, scripts y workflows, y al fallar no repite el texto
        encontrado. Se prueba con patrones inventados que casan con textos fijos de esos ficheros, nunca con la lista
        real."""
        marcas = [("CHANGELOG.md", r"^# Cambios"), ("README.md", r"^# Tenca"), ("LEEME.txt", r"^tenca · grabar"),
                  ("tests/run.py", r"MIN_TESTS = \d+"), ("scripts/build_zip.py", r"def version_string\("),
                  (".github/workflows/tests.yml", r"^name: tests\b")]
        encontrados = []
        for rel, patron in marcas:  # si cambia uno de esos ficheros, hay que cambiar su marca
            m = re.search(patron, self.read(*rel.split("/")).decode("utf-8-sig"), re.I)
            self.assertTrue(m, f"{patron!r} ya no casa con {rel}")
            encontrados.append(m.group(0))
        lista = ",".join(p for _, p in marcas[:3]) + "\n" + "\n".join(p for _, p in marcas[3:])  # comas y líneas
        resultado = unittest.TestResult()
        with mock.patch.dict(os.environ, {"NOTAS_PALABRAS_VETADAS": lista}):
            TemplatesTests("test_sin_rastro_de_clientes").run(resultado)
        self.assertEqual((resultado.testsRun, resultado.skipped, resultado.errors), (1, [], []))
        self.assertEqual(len(resultado.failures), 1)
        informe = resultado.failures[0][1]
        for k, (rel, _) in enumerate(marcas, 1):
            self.assertRegex(informe, r"(?m)^  " + re.escape(rel) + r":\d+ \(patrón " + str(k) + r"\)$")
        for texto in encontrados:
            self.assertNotIn(texto, informe)

    def test_agnosticismo_sin_git(self):
        """Sin git (una copia descargada sin .git) recorre los ficheros de la raíz del repo y _app, tests, scripts,
        .github y design, sin __pycache__, Vocabulario, binarios ni las carpetas de reuniones."""
        root = tempfile.mkdtemp(prefix="chuleta-agnostico-")
        ficheros = {"README.md": "marca", "CHANGELOG.md": "", "LEEME.txt": "", "_app/a.py": "", "_app/static/b.js": "",
                    "tests/c.py": "x\nmarca", "scripts/e2e/d.ps1": "", ".github/workflows/e.yml": "marca y marca",
                    "design/f.svg": "", "_app/__pycache__/g.py": "marca", "_app/Vocabulario/h.txt": "marca",
                    "Proyecto Alfa/reunion.md": "marca", "reunion suelta.srt": "marca"}
        for rel, texto in ficheros.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
                f.write(texto)
        with open(os.path.join(root, "design", "logo.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\0\0marca")
        rels = ficheros_del_repo(root)
        self.assertEqual(set(rels), {"README.md", "CHANGELOG.md", "LEEME.txt", "_app/a.py", "_app/static/b.js",
                                     "tests/c.py", "scripts/e2e/d.ps1", ".github/workflows/e.yml", "design/f.svg",
                                     "design/logo.png"})
        self.assertEqual(buscar_palabras(["MARCA"], rels, root),
                         [(".github/workflows/e.yml", 1, 1), ("README.md", 1, 1), ("tests/c.py", 2, 1)])
        with self.assertRaises(ValueError) as cm:  # un patrón roto se dice por su número, sin enseñarlo
            buscar_palabras(["ok", "(sin cerrar"], rels, root)
        self.assertNotIn("sin cerrar", str(cm.exception))
        self.assertIn("patrón 2", str(cm.exception))

    def test_agnosticismo_exigido_sin_lista_falla(self):
        """Con NOTAS_EXIGIR_VETADAS (en local, para no dar por revisado lo que se ha saltado) una lista que falta o está
        vacía hace fallar el test en vez de saltarlo; sin ella, como en la CI, se salta."""
        casos = (({"NOTAS_EXIGIR_VETADAS": "true"}, "falla"),
                 ({"NOTAS_EXIGIR_VETADAS": "1", "NOTAS_PALABRAS_VETADAS": " , \n "}, "falla"),
                 ({"NOTAS_EXIGIR_VETADAS": "false", "NOTAS_PALABRAS_VETADAS": ""}, "salta"),  # la CI sin el secreto
                 ({}, "salta"))  # en local, sin la lista
        for env, esperado in casos:
            with self.subTest(env=env):
                entorno = {k: v for k, v in os.environ.items()
                           if k not in ("NOTAS_EXIGIR_VETADAS", "NOTAS_PALABRAS_VETADAS")}
                entorno.update(env)
                resultado = unittest.TestResult()
                with mock.patch.dict(os.environ, entorno, clear=True):
                    TemplatesTests("test_sin_rastro_de_clientes").run(resultado)
                self.assertEqual((resultado.testsRun, resultado.errors), (1, []))
                if esperado == "falla":
                    self.assertEqual((len(resultado.failures), resultado.skipped), (1, []))
                    self.assertIn("NOTAS_EXIGIR_VETADAS", resultado.failures[0][1])
                else:
                    self.assertEqual((resultado.failures, len(resultado.skipped)), ([], 1))

    def test_agnosticismo_sin_lista_avisa_si_se_pide(self):
        """Con NOTAS_AVISAR_SIN_VETADAS (el paso de tests.yml), una lista sin ningún patrón deja en la salida estándar,
        en una línea, el ::warning que GitHub enseña en el resumen del run, y el test se salta. Lo decide la misma
        palabras_vetadas() que lee la lista, también con blancos que no son ASCII (str.strip() quita \\x1c o el espacio
        duro, y bash con [[:space:]] no los veía). Sin ella (en local y en los jobs de la matriz) se salta callado."""
        for lista in (None, "", " , \n\t,", "\x1c,\xa0\n\u202f\x85"):
            for avisar in ("1", None):
                with self.subTest(lista=lista, avisar=avisar):
                    entorno = {k: v for k, v in os.environ.items() if not k.startswith("NOTAS_")}
                    if lista is not None:
                        entorno["NOTAS_PALABRAS_VETADAS"] = lista
                    if avisar:
                        entorno["NOTAS_AVISAR_SIN_VETADAS"] = avisar
                    resultado = unittest.TestResult()
                    with mock.patch.dict(os.environ, entorno, clear=True), \
                            mock.patch("sys.stdout", new_callable=io.StringIO) as salida:
                        TemplatesTests("test_sin_rastro_de_clientes").run(resultado)
                    self.assertEqual((resultado.testsRun, resultado.failures, resultado.errors, len(resultado.skipped)),
                                     (1, [], [], 1))
                    self.assertEqual(salida.getvalue(), AVISO_SIN_VETADAS + "\n" if avisar else "")
        self.assertTrue(AVISO_SIN_VETADAS.startswith("::warning title=Agnosticismo sin ejecutar::"))
        self.assertNotRegex(AVISO_SIN_VETADAS, "[%\r\n]")  # GitHub cortaría el mensaje (o habría que escaparlos)
        for texto in ("no se ha ejecutado", 'NOTAS_PALABRAS_VETADAS="$(cat ~/tenca-vetadas.txt)" .venv/bin/python '
                                            "tests/run.py"):
            self.assertIn(texto, AVISO_SIN_VETADAS)

    def test_agnosticismo_ancla_por_linea(self):
        """^ y $ de un patrón marcan el principio y el fin de cada línea, no solo los del fichero, también en los
        ficheros con CRLF (.bat, .ps1 y LEEME.txt)."""
        root = tempfile.mkdtemp(prefix="chuleta-agnostico-")
        rels = ["a.txt", "b.bat", "c.md"]
        for rel, datos in zip(rels, (b"uno\nmarca\n", b"uno\r\nmarca\r\nfin\r\n", b"no marca\n")):
            with open(os.path.join(root, rel), "wb") as f:
                f.write(datos)
        self.assertEqual(buscar_palabras(["^marca$"], rels, root), [("a.txt", 2, 1), ("b.bat", 2, 1)])
        self.assertEqual(buscar_palabras(["marca$"], rels, root), [("a.txt", 2, 1), ("b.bat", 2, 1), ("c.md", 1, 1)])


class NotasInternasTests(unittest.TestCase):
    """El repo es público: nada de notas de trabajo internas en ningún fichero versionado. A diferencia de
    test_sin_rastro_de_clientes, los patrones son genéricos y públicos (NOTAS_INTERNAS), así que corre siempre, sin
    secreto: en la suite de cada plataforma y en su propio paso del job `agnosticismo` de tests.yml."""

    def test_sin_notas_internas(self):
        """Sin referencias a mensajes («msg» con su número), sin el nombre del autor fuera de la licencia, de los
        identificadores que no pueden cambiar y del editor del instalador (NOTAS_EXENTAS), sin el nombre del asistente
        que coordinaba el trabajo ni del sistema de agentes y sin rutas personales de macOS."""
        hits = buscar_notas_internas(ficheros_del_repo())
        if hits:
            lineas = [f"  {rel}:{n} ({tipo}: {texto!r})" for rel, n, tipo, texto in hits[:80]]
            if len(hits) > 80:
                lineas.append(f"  … y {len(hits) - 80} más")
            self.fail(f"{len(hits)} notas internas en el repo público (reescríbelas con el porqué técnico, sin "
                      "quién lo pidió ni dónde):\n" + "\n".join(lineas))

    def test_notas_internas_casa_y_respeta_las_exentas(self):
        """Los patrones casan con cada tipo de nota (también en mayúsculas, en un fichero CRLF, en el CHANGELOG y con la
        referencia partida por el ajuste de línea de un comentario, también en un .js de _app/static/) y dejan pasar lo
        exento: LICENSE, el AppUserModelID, la página de PayPal.me del autor y, solo en los ficheros del instalador y
        sus tests, el editor como valor literal entero; el nombre completo entre comillas en un dato de prueba
        cualquiera sí sale, y otra página de PayPal.me con su nombre, también."""
        root = tempfile.mkdtemp(prefix="tenca-notas-")
        nombre, apellido = "Ber" + "nardo", "Stegm" + "aier"  # en trozos: el propio test se revisa a sí mismo
        asistente, sistema = "Clau" + "dia", "Open" + "Claude"
        ficheros = {
            "CHANGELOG.md": f"uno\nlo pidió (ms{''}g 1479)\nms{''}gs 1150-1152 y MS{''}G 1386\n",
            "_app/a.py": f"# en el portátil de {nombre}\nx = 1  # decisión de {asistente}\n"
                         f"# (fix-proveedor, ms{''}gs\n#   1468)\n# revisión 1 (Fa{''}ble 5.2)\n",
            "tests/b.py": f'ruta = "/Us{""}ers/alguien/Projects/tenca"\nusers = "/users/x"  # minúsculas: una URL\n'
                          f'apodo = "{nombre[:-2]}"\n',
            "_app/instalar.ps1": f'$AppId = "{nombre}.Chuleta"\r\n$Publisher = "{nombre} {apellido}"\r\n'
                                 f'Check "x" ($l -match "{nombre}\\.Chuleta")\r\n{nombre} {apellido} lo pidió\r\n'
                                 f'x = "{nombre} {apellido} lo validó"\r\nautor={nombre} {apellido} decidió\r\n',
            "scripts/setup/tenca.iss": f"AppPublisher={nombre} {apellido}\n"
                                       f"VersionInfoCopyright={nombre} {apellido} (licencia MIT)\n",
            "tests/t1.py": f'yo = v.create("{nombre} {apellido}", es_usuario=True)\n',  # fuera de los del instalador
            "README.md": f"el daemon de {sistema}\nmsgs NNNN y una versión 2026.11 sin número de mensaje\n"
                         f"notas en memory/proj{''}ects/x_IMPL.md y ~/Proj{''}ects/notas\n"
                         f"[un café](https://paypal.me/{nombre.lower()}steg)\notra: paypal.me/{nombre.lower()}\n",
            "LICENSE": f"Copyright (c) 2026 {nombre} {apellido}\n",
            "_app/static/e.js": f"// ms{''}g 1242 de {nombre}\n",
        }
        for rel, texto in ficheros.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as f:
                f.write(texto)
        hits = [(rel, n, tipo) for rel, n, tipo, _ in buscar_notas_internas(sorted(ficheros), root)]
        autor = "nombre del autor"
        self.assertEqual(hits, [("CHANGELOG.md", 2, "mensaje"), ("CHANGELOG.md", 3, "mensaje"),
                                ("CHANGELOG.md", 3, "mensaje"), ("README.md", 1, "sistema de agentes"),
                                ("README.md", 3, "ruta personal"), ("README.md", 3, "ruta personal"),
                                ("README.md", 5, autor),
                                ("_app/a.py", 1, autor), ("_app/a.py", 2, "asistente"), ("_app/a.py", 3, "mensaje"),
                                ("_app/a.py", 5, "asistente")]
                         + [("_app/instalar.ps1", n, autor) for n in (4, 4, 5, 5, 6, 6)]
                         + [("_app/static/e.js", 1, "mensaje"), ("_app/static/e.js", 1, autor)]
                         + [("tests/b.py", 1, "ruta personal"), ("tests/b.py", 3, autor)]
                         + [("tests/t1.py", 1, autor), ("tests/t1.py", 1, autor)])

    def test_sin_proveedor_fijo(self):
        """Ningún proveedor de la nube por delante de los demás: el que se quitó de la documentación y de los textos de
        la app (PROVEEDOR_QUITADO) no vuelve fuera de su tabla (engines.PROVIDERS y CHAT_URLS) ni de sus
        identificadores (buscar_proveedor_fijo dice dónde puede estar)."""
        hits = buscar_proveedor_fijo(ficheros_del_repo())
        if hits:
            lineas = [f"  {rel}:{n} ({tipo}: {texto!r})" for rel, n, tipo, texto in hits[:80]]
            if len(hits) > 80:
                lineas.append(f"  … y {len(hits) - 80} más")
            self.fail(f"{len(hits)} menciones de un proveedor concreto fuera de su tabla (en los textos, «tu API», «el "
                      "proveedor» o su nombre por engines.provider_name):\n" + "\n".join(lineas))

    def test_proveedor_fijo_casa_y_respeta_la_tabla(self):
        """Casa con el nombre en cada sitio publicado, en mayúsculas o minúsculas (también en una URL, en un comentario
        o docstring, en un fichero CRLF, en una f-string, también dentro de sus llaves, entre comillas en el JS, en
        design/ y en los .bat de la raíz) y deja pasar la tabla de
        proveedores, los identificadores («groq» entre comillas, una variable de entorno, el nombre de un test, también
        dentro de una frase o de la expresión de una f-string) y los tests y scripts, que no son texto publicado (sus
        datos de prueba configuran ese proveedor)."""
        root = tempfile.mkdtemp(prefix="tenca-proveedor-")
        p = "Gr" + "oq"  # en trozos: el propio test no lo nombra
        ident, mayus = p.lower(), p.upper()
        ficheros = {
            "README.md": f"uno\ncon {p} o con OpenAI\nmira api.{ident}.com\n",
            "LEEME.txt": f"uno\r\nlímites de {p}\r\n",
            "CHANGELOG.md": f"- `tests/test_sin_{ident}.py`\n- nada\n",
            ".github/workflows/w.yml": (f"# un {p} falso\nrun: python -m unittest test_sin_{ident} test_fix_{ident}\n"
                                        f"env:\n  CHULETA_PRUEBAS_URL_{mayus}: x\n# mira console.{ident}.com\n"
                                        f"name: tests de {ident}\n"),
            ".github/dependabot.yml": f"# {ident}\n",
            "INSTALAR.bat": f"echo uno\r\necho con {p}\r\n",
            "_app/static/x.js": (f"const prov = c.provider || '{ident}';\nif (p !== \"{ident}\") t = `{p} pide`;\n"
                                 f"// el cupo de {p}\nconst u = 'https://console.{ident}.com';\n"
                                 f"const n = prov || '{p}';\n"),
            "_app/static/x.html": f"<p>{p}</p>\n",
            "_app/a.py": (f'"""Modulo: con {p} o sin el."""\nC = {{"provider": "{ident}"}}  # rama fix-{ident}\n'
                          f'E = "CHULETA_PRUEBAS_URL_{mayus}"\nm = f"{p} pide esperar {{n}} s"\n'
                          f'# cupo de {p}\n\n\ndef f():\n    """cloud.provider "{ident}" por defecto."""\n'
                          f'    return "pasa a {p}"\n'
                          f'log("PRUEBAS: va a una URL de pruebas (CHULETA_PRUEBAS_URL_{mayus})")\n'
                          f'M = "{mayus}"\n'
                          f'g = f"{{c.get(\'provider\', \'{ident}\')}}: hecho"\n'
                          f'# ver console.{ident}.com\n'
                          f'h = f"cupo de {ident} hoy"\n'
                          f'k = f"{{prov or \'{p}\'}} pide"\n'),
            "_app/engines.py": (f'PROVIDERS = {{\n    "{ident}": {{"label": "{p}", "url": "https://x.{ident}.com"}},\n'
                                f'}}\nCHAT_URLS = {{"{ident}": "https://api.{ident}.com/chat"}}\nX = "{p}"\n'),
            "tests/t.py": f'class {p}Falso:\n    """Un {p} falso."""\n',
            "scripts/s.py": f'URL = "https://api.{ident}.com"  # {p}\n',
            "design/d.py": f"# {p}\n",
        }
        for rel, texto in ficheros.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as f:
                f.write(texto)
        hits = [(rel, n, tipo) for rel, n, tipo, _ in buscar_proveedor_fijo(sorted(ficheros), root)]
        w = ".github/workflows/w.yml"
        self.assertEqual(hits, [(".github/dependabot.yml", 1, "publicado"), (w, 1, "publicado"), (w, 5, "publicado"),
                                (w, 6, "publicado"), ("CHANGELOG.md", 1, "documento"), ("INSTALAR.bat", 2, "publicado"),
                                ("LEEME.txt", 2, "documento"), ("README.md", 2, "documento"),
                                ("README.md", 3, "documento")]
                         + [("_app/a.py", n, tipo) for n, tipo in (
                             (1, "docstring"), (2, "comentario"), (4, "texto"), (5, "comentario"), (10, "texto"),
                             (12, "texto"), (14, "comentario"), (15, "texto"), (16, "texto"))]
                         + [("_app/engines.py", 5, "texto"), ("_app/static/x.html", 1, "publicado")]
                         + [("_app/static/x.js", n, "publicado") for n in (2, 3, 4, 5)]
                         + [("design/d.py", 1, "comentario")])


# Ningún proveedor de la nube por delante de los demás (16/11/2026). El nombre del que se quitó de la documentación y
# de los textos de la app, con una letra entre corchetes para no casar consigo mismo en este fichero. Puede estar en la
# tabla de proveedores (PROVEEDOR_TABLA, la única que lo nombra) y como identificador (PROVEEDOR_IDS): «groq» entre
# comillas (el valor de cloud.provider) o una palabra unida con «_» (una variable de entorno, el nombre de un test),
# también dentro de una frase. En los .py de _app, una cadena que es entera un identificador en minúsculas («groq»)
# tampoco cuenta. Los tests y los scripts de desarrollo no son texto publicado: sus datos de prueba configuran ese
# proveedor.
PROVEEDOR_QUITADO = re.compile(r"gr[o]q", re.I)
PROVEEDOR_IDS = re.compile(r"(['\"])gr[o]q\1|(?i:\b\w*_gr[o]q\w*\b|\bgr[o]q_\w*\b)")  # entre comillas, en minúsculas
PROVEEDOR_ID_PY = re.compile(r"^[a-z0-9_]*gr[o]q[a-z0-9_]*$")  # una cadena de Python que es entera el identificador
PROVEEDOR_DOCS = ("README.md", "LEEME.txt", "_app/export/LEEME.txt", "CHANGELOG.md")
PROVEEDOR_TABLA = {"_app/engines.py": ("PROVIDERS", "CHAT_URLS")}


def _proveedor_menciones(texto):
    """Las menciones de PROVEEDOR_QUITADO en `texto` que no son un identificador (PROVEEDOR_IDS): [(posición, texto)].
    Los identificadores se tapan con el mismo largo, así que las posiciones no cambian."""
    texto = PROVEEDOR_IDS.sub(lambda m: "·" * len(m.group(0)), texto)
    return [(m.start(), m.group(0)) for m in PROVEEDOR_QUITADO.finditer(texto)]


def _cadenas_de_token(token):
    """Las cadenas de un token STRING: su valor o, en una f-string (un solo token antes de Python 3.12), sus trozos de
    texto juntos y, aparte, cada cadena de las expresiones de dentro de las llaves (como las ve Python 3.12)."""
    import ast
    try:
        nodo = ast.parse(token.strip(), mode="eval").body
    except SyntaxError:
        return [token]
    if isinstance(nodo, ast.JoinedStr):
        texto = "".join(v.value for v in nodo.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        dentro = [c.value for v in nodo.values if isinstance(v, ast.FormattedValue)
                  for c in ast.walk(v) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        return [texto] + dentro
    if isinstance(nodo, ast.Constant):
        return [nodo.value.decode("utf-8", "replace") if isinstance(nodo.value, bytes) else str(nodo.value)]
    return [token]


def _proveedor_en_python(rel, text):
    """[(línea, tipo, texto)] de un .py de _app: cualquier mención que no sea un identificador en los comentarios, los
    docstrings y las cadenas (lo literal de las f-strings), salvo una cadena que es entera el identificador y lo que
    está dentro de la tabla de proveedores."""
    import ast
    import tokenize
    tree = ast.parse(text)
    docs, tabla = set(), set()
    for node in ast.walk(tree):
        cuerpo = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and cuerpo
                and isinstance(cuerpo[0], ast.Expr) and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)):
            docs.add(cuerpo[0].lineno)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in PROVEEDOR_TABLA.get(rel, ())
                                                for t in node.targets):
            tabla.update(range(node.lineno, node.end_lineno + 1))
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        n = tok.start[0]
        if n in tabla:
            continue
        if tok.type == tokenize.COMMENT:
            tipo, valores = "comentario", [tok.string]
        elif tok.type == tokenize.STRING:
            tipo, valores = ("docstring" if n in docs else "texto"), _cadenas_de_token(tok.string)
        elif tok.type == getattr(tokenize, "FSTRING_MIDDLE", None):  # Python 3.12 o más: el texto de una f-string
            tipo, valores = "texto", [tok.string]
        else:
            continue
        for valor in valores:
            if not (tipo == "texto" and PROVEEDOR_ID_PY.match(valor)):
                out += [(n, tipo, t) for _, t in _proveedor_menciones(valor)]
    return out


def buscar_proveedor_fijo(rels, root=REPO):
    """[(fichero, línea, tipo, texto)] de cada mención de PROVEEDOR_QUITADO donde no puede estar: cualquiera en los
    documentos publicados (PROVEEDOR_DOCS); en los .py, lo de _proveedor_en_python; y en todo lo demás que se publica
    con la app o en el repo (_app/ entero, también la interfaz de _app/static/; .github/; design/; los ficheros de la
    raíz, como los .bat), cualquiera que no sea un identificador (PROVEEDOR_IDS). Los tests y los scripts de desarrollo
    no cuentan."""
    import bisect
    hits = []
    for rel in rels:
        path = os.path.join(root, rel)
        en_docs = rel in PROVEEDOR_DOCS
        publicado = rel.startswith(("_app/", ".github/", "design/")) or "/" not in rel  # la raíz: .bat, make_icons…
        if not (en_docs or publicado) or not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data[:8192]:
            continue  # binario (iconos, imágenes)
        text = data.decode("utf-8-sig", "replace").replace("\r\n", "\n")
        if not PROVEEDOR_QUITADO.search(text):
            continue
        if en_docs:
            encontrados, tipo = [(m.start(), m.group(0)) for m in PROVEEDOR_QUITADO.finditer(text)], "documento"
        elif rel.endswith(".py"):
            hits += [(rel, n, tipo, t) for n, tipo, t in _proveedor_en_python(rel, text)]
            continue
        else:
            encontrados, tipo = _proveedor_menciones(text), "publicado"
        saltos = [m.start() for m in re.finditer("\n", text)]
        hits += [(rel, bisect.bisect_left(saltos, i) + 1, tipo, t) for i, t in encontrados]
    return sorted(hits)


# Notas internas que no van en un repo público (punto 50 del review del 14/11/2026). Cada patrón lleva una letra entre
# corchetes para no casar consigo mismo en este fichero. Mira todo lo versionado, también _app/static/ (que además
# tiene su propia comprobación en test_modulos).
NOTAS_INTERNAS = (
    # también partida por el ajuste de línea de un comentario («msgs» y, en la línea siguiente, «# 1468»)
    ("mensaje", re.compile(r"\bms[g]s?\.?[\s#;/*]*\d{3,4}\b", re.I)),
    ("nombre del autor", re.compile(r"\bber[n]ard|\bber[n]ar\b|\bstegm[a]ier\b", re.I)),
    ("asistente", re.compile(r"\bclau[d]ia\b|\bfa[b]le\s*\d", re.I)),  # y el modelo que hizo una revisión
    ("sistema de agentes", re.compile(r"\bopencla[u]de\b", re.I)),
    # la carpeta de usuarios de macOS, con su mayúscula (en minúsculas es parte de muchas URL), y las notas de trabajo
    # que viven fuera del repo
    ("ruta personal", re.compile(r"/Us[e]rs/|\bmemory/proj[e]cts/|~/Proj[e]cts\b")),
)
NOTAS_FICHEROS_EXENTOS = ("LICENSE",)
# Lo único que puede llevar el nombre del autor, porque cambiarlo cambiaría la app: el AppUserModelID de Windows
# (common.APP_ID; los accesos directos ya creados y la barra de tareas lo llevan, ver test_tenca) y el que usó la
# prueba de WebView2 de scripts/, también con la barra escapada de una expresión regular de PowerShell, en cualquier
# fichero; y el editor, solo en los ficheros del instalador y sus tests (NOTAS_FICHEROS_EDITOR) y como valor literal
# entero: justo detrás de una comilla o de un «=» y seguido de la comilla de cierre, del fin de la línea o de
# «(licencia MIT)» (Publisher del registro, con el que desinstalar.ps1 reconoce sus entradas, y AppPublisher/VersionInfo
# del Setup.exe), igual que en LICENSE. «x = "<nombre> lo validó"» no está exento, ni el nombre completo entre
# comillas en otro fichero (un dato de prueba con la persona real). También, en cualquier fichero, su página de
# PayPal.me (paquete.DONAR_URL, en el README y en el LEEME): es la dirección de su cuenta de PayPal, y con otra la
# invitación no le llegaría.
NOTAS_EXENTAS = re.compile(r"\bBer[n]ardo\\?\.(?:Chuleta|Notas)\b|\bpaypal\.me/ber[n]ardosteg\b")
NOTAS_EXENTAS_EDITOR = re.compile(r"(?<=[\"'=])Ber[n]ardo Stegm[a]ier(?=[\"']|[ \t]*$|[ \t]+\(licencia MIT\))", re.M)
NOTAS_FICHEROS_EDITOR = ("_app/instalar.ps1", "_app/desinstalar.ps1", "scripts/setup/tenca.iss",
                         "scripts/e2e/lookfeel_e2e.ps1", "tests/test_desinstalar.py", "tests/test_tenca.py")


def buscar_notas_internas(rels, root=REPO):
    """[(fichero, línea, tipo, texto)] de cada nota interna de NOTAS_INTERNAS en los ficheros `rels` (relativos a
    `root`, con «/»), saltando binarios, NOTAS_FICHEROS_EXENTOS, lo que casa con NOTAS_EXENTAS
    y, en NOTAS_FICHEROS_EDITOR, con NOTAS_EXENTAS_EDITOR. Ordenado por fichero, línea y posición."""
    import bisect
    hits = []
    for rel in rels:
        if rel in NOTAS_FICHEROS_EXENTOS:
            continue
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue  # en el índice de git pero borrado de la copia de trabajo
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data[:8192]:
            continue  # binario (iconos, imágenes, audios de prueba)
        text = data.decode("utf-8-sig", "replace").replace("\r\n", "\n")
        tapar = lambda m: "·" * len(m.group(0))  # noqa: E731  # mismo largo: no cambia ninguna posición
        text = NOTAS_EXENTAS.sub(tapar, text)
        if rel in NOTAS_FICHEROS_EDITOR:
            text = NOTAS_EXENTAS_EDITOR.sub(tapar, text)
        saltos = [m.start() for m in re.finditer("\n", text)]
        encontrados = sorted((m.start(), tipo, m.group(0)) for tipo, rx in NOTAS_INTERNAS for m in rx.finditer(text))
        hits += [(rel, bisect.bisect_left(saltos, i) + 1, tipo, texto) for i, tipo, texto in encontrados]
    return hits


def palabras_vetadas():
    """La lista privada de NOTAS_PALABRAS_VETADAS: expresiones regulares separadas por comas o por saltos de línea."""
    return [p.strip() for p in re.split(r"[,\n]", os.environ.get("NOTAS_PALABRAS_VETADAS", "")) if p.strip()]


def vetadas_exigidas():
    """NOTAS_EXIGIR_VETADAS («true» o «1»): sin lista, el test de agnosticismo falla en vez de saltarse. Es para
    ejecutarlo en local: tests.yml no la pone (sin el secreto, el test avisa y no falla). Para exigir el secreto en la
    CI iría en ese paso, solo en los push del repo propio (un fork y Dependabot no reciben los secretos), y habría que
    cambiar también AgnosticismoCITests (tests/test_ci.py)."""
    return os.environ.get("NOTAS_EXIGIR_VETADAS", "").strip().lower() in ("true", "1")


AVISO_SIN_VETADAS = (
    "::warning title=Agnosticismo sin ejecutar::El test de agnosticismo no se ha ejecutado: no ha llegado la lista "
    "del secreto NOTAS_PALABRAS_VETADAS o no trae ningún patrón (no existe o está vacío en Settings -> Secrets and "
    "variables -> Actions, o el run es de un fork o de Dependabot, que no reciben los secretos). En local, con la "
    "lista en un fichero fuera del repo: "
    'NOTAS_EXIGIR_VETADAS=1 NOTAS_PALABRAS_VETADAS="$(cat ~/tenca-vetadas.txt)" .venv/bin/python tests/run.py '
    "(en Windows, ver test_sin_rastro_de_clientes en tests/test_package.py)")


def avisar_sin_vetadas():
    """NOTAS_AVISAR_SIN_VETADAS («true» o «1»), solo en el paso de tests.yml: sin lista, el test de agnosticismo deja
    AVISO_SIN_VETADAS en la salida estándar (GitHub lo enseña en el resumen del run) antes de saltarse."""
    return os.environ.get("NOTAS_AVISAR_SIN_VETADAS", "").strip().lower() in ("true", "1")


_RAIZ_SIN_GIT = ("CHANGELOG.md", "README.md", "LEEME.txt", "LICENSE", "INSTALAR.bat", "ABRIR TENCA.bat",
                 "make_icons.py", "requirements.txt", "requirements-dev.txt", "setup.cfg", ".gitignore",
                 ".gitattributes")


def ficheros_del_repo(root=REPO):
    """Lo que publica el repo, relativo a `root` y con «/»: `git ls-files`. Sin git, los ficheros conocidos de la raíz
    y todo _app, tests, scripts, .github y design, sin __pycache__ ni Vocabulario (las carpetas de reuniones que haya
    en la raíz no son del repo)."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # p. ej. GIT_DIR desde un hook de git
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, env=env, timeout=60)
        if r.returncode == 0 and r.stdout.strip(b"\0"):
            return sorted(os.fsdecode(p) for p in r.stdout.split(b"\0") if p)
    except (OSError, subprocess.SubprocessError):
        pass
    rels = [n for n in _RAIZ_SIN_GIT if os.path.isfile(os.path.join(root, n))]
    for top in ("_app", "tests", "scripts", ".github", "design"):
        for d, dirs, names in os.walk(os.path.join(root, top)):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "Vocabulario")]
            rels += [os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/") for n in names]
    return sorted(rels)


def buscar_palabras(patrones, rels, root=REPO):
    """[(fichero, línea, n.º de patrón)] de cada línea donde empieza una coincidencia de un patrón (sin distinguir
    mayúsculas; ^ y $ son el principio y el fin de cada línea), saltando los binarios. Nunca devuelve el texto
    encontrado: es justo lo que no puede acabar en un log público."""
    import bisect
    regs = []
    for k, p in enumerate(patrones, 1):
        try:
            regs.append(re.compile(p, re.I | re.M))
        except re.error:
            raise ValueError(f"el patrón {k} de NOTAS_PALABRAS_VETADAS no es una expresión regular válida") from None
    hits = []
    for rel in rels:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue  # en el índice de git pero borrado de la copia de trabajo
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data[:8192]:
            continue  # binario (iconos, imágenes)
        text = data.decode("utf-8-sig", "replace").replace("\r\n", "\n")  # con CRLF, el \r no deja casar $
        saltos = None
        for k, rx in enumerate(regs, 1):
            inicios = [m.start() for m in rx.finditer(text)]
            if inicios:
                if saltos is None:
                    saltos = [m.start() for m in re.finditer("\n", text)]
                hits += [(rel, n, k) for n in sorted({bisect.bisect_left(saltos, i) + 1 for i in inicios})]
    return hits


class BuildZipScriptTests(unittest.TestCase):
    def test_script_genera_el_zip_instalable(self):
        import subprocess
        import sys
        dest = os.path.join(tempfile.mkdtemp(prefix="chuleta-dist-"), "Tenca - instalador.zip")
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_zip.py"), dest],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        with zipfile.ZipFile(dest) as z:
            names = set(z.namelist())
            self.assertIn("Tenca/INSTALAR.bat", names)
            self.assertIn("Tenca/ABRIR TENCA.bat", names)
            self.assertIn("Tenca/LEEME.txt", names)
            self.assertIn("Tenca/LICENSE", names)
            self.assertIn("Tenca/_app/server.py", names)
            self.assertIn("Tenca/_app/version.txt", names)
            self.assertIn("Tenca/_app/static/app.js", names)
            self.assertIn("Tenca/_app/export/LEEME.txt", names)
            self.assertFalse(any("Vocabulario" in n for n in names))
            version = z.read("Tenca/_app/version.txt").strip()
            self.assertRegex(version, rb"^" + VERSION_RE + rb"$")
            leeme = z.read("Tenca/LEEME.txt")
            self.assertIn(version, leeme.split(b"\r\n")[0])
            self.assertNotIn(b"{FECHA}", leeme)
            self.assertEqual(z.testzip(), None)
        self.assertIn("versión " + version.decode("utf-8"), r.stdout)
        with open(os.path.join(REPO, "_app", "version.txt"), "rb") as f:  # también en el repo (server.py lo enseña)
            self.assertEqual(f.read().strip(), version)

    def test_version_string(self):
        """«AAAA-MM-DD · sha» con git; sin git (o sin commits), la fecha de hoy a secas."""
        bz = load_build_zip()
        with mock.patch.object(bz, "git", side_effect=lambda *a: {"log": "2026-05-06", "rev-parse": "abc1234"}[a[0]]):
            self.assertEqual(bz.version_string(), "2026-05-06 · abc1234")
        with mock.patch.object(bz, "git", return_value=""):
            self.assertEqual(bz.version_string(), time.strftime("%Y-%m-%d"))
        if os.path.isdir(os.path.join(REPO, ".git")) or os.path.isfile(os.path.join(REPO, ".git")):
            self.assertRegex(bz.version_string(), r"^\d{4}-\d{2}-\d{2} · [0-9a-f]{7,12}$")

    def test_vocab_solo_si_se_pide(self):
        import subprocess
        import sys
        folder = tempfile.mkdtemp(prefix="chuleta-vocab-")
        os.makedirs(os.path.join(folder, "Proyecto Alfa", "Vocabulario"))
        os.makedirs(os.path.join(folder, "_app", "Vocabulario"))  # carpetas de sistema: no cuentan
        with open(os.path.join(folder, "Proyecto Alfa", "Vocabulario", "General.txt"), "w", encoding="utf-8") as f:
            f.write("# siglas\nAPI, SLA\n")
        with open(os.path.join(folder, "_app", "Vocabulario", "no.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        dest = os.path.join(folder, "privado.zip")
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_zip.py"), dest, "--vocab", folder],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        with zipfile.ZipFile(dest) as z:
            names = set(z.namelist())
            self.assertIn("Tenca/Proyecto Alfa/Vocabulario/General.txt", names)
            self.assertEqual([n for n in names if "Vocabulario" in n],
                             ["Tenca/Proyecto Alfa/Vocabulario/General.txt"])
            body = z.read("Tenca/Proyecto Alfa/Vocabulario/General.txt")  # build_package deja los .txt con BOM y CRLF
            self.assertEqual(body.decode("utf-8-sig").splitlines(), ["# siglas", "API, SLA"])
        self.assertIn("1 listas de vocabulario", r.stdout)


if __name__ == "__main__":
    unittest.main()
