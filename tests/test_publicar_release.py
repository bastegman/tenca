# -*- coding: utf-8 -*-
"""scripts/publicar_release.py: el zip que se publica no lleva listas de vocabulario ni nada fuera de git, y las notas
llevan el sha256 y lo nuevo del CHANGELOG."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import publicar_release as pr  # noqa: E402

VERSIONADOS = ["_app/server.py", "_app/instalar.ps1", "_app/static/app.js", "_app/static/fuentes-OFL.txt",
               "_app/export/INSTALAR.bat", "_app/export/LEEME.txt", "LICENSE", "LEEME.txt", "README.md"]
LIMPIO = ["Tenca/INSTALAR.bat", "Tenca/LEEME.txt", "Tenca/LICENSE", "Tenca/_app/server.py",
          "Tenca/_app/instalar.ps1", "Tenca/_app/static/app.js", "Tenca/_app/static/fuentes-OFL.txt",
          "Tenca/_app/export/INSTALAR.bat", "Tenca/_app/export/LEEME.txt", "Tenca/_app/version.txt"]


def hay_git():
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", "HEAD^{commit}"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class ZipTests(unittest.TestCase):
    def test_zip_limpio(self):
        self.assertEqual(pr.problemas_zip(LIMPIO, VERSIONADOS), [])

    def test_carpeta_vocabulario(self):
        for n in ("Tenca/Proyecto Alfa/Vocabulario/clientes.txt", "Tenca/x/VOCABULARIO/a.md", "Tenca/vocabulario/"):
            malos = pr.problemas_zip(LIMPIO + [n], VERSIONADOS + [n[len("Tenca/"):]])
            self.assertEqual(len(malos), 1, malos)
            self.assertIn("Vocabulario", malos[0])

    def test_txt_que_no_es_de_la_app(self):
        malos = pr.problemas_zip(LIMPIO + ["Tenca/_app/static/nombres.txt"], VERSIONADOS + ["_app/static/nombres.txt"])
        self.assertEqual(len(malos), 1, malos)
        self.assertIn(".txt", malos[0])

    def test_fichero_fuera_de_git(self):
        malos = pr.problemas_zip(LIMPIO + ["Tenca/_app/borrador.py"], VERSIONADOS)
        self.assertEqual(len(malos), 1, malos)
        self.assertIn("no está en git", malos[0])
        self.assertTrue(pr.problemas_zip(LIMPIO + ["Tenca/tests/test_x.py"], VERSIONADOS + ["tests/test_x.py"]))
        # una plantilla en la raíz solo vale si está en _app/export
        self.assertTrue(pr.problemas_zip(LIMPIO + ["Tenca/README.md"], VERSIONADOS))

    def test_rutas_peligrosas_y_raiz(self):
        for n in ("../fuera.py", "/abs.py", "C:/x.py", "Tenca/../x.py", "Otra/INSTALAR.bat"):
            self.assertTrue(pr.problemas_zip(LIMPIO + [n], VERSIONADOS), n)

    def test_repetidos_y_carpetas(self):
        self.assertEqual(pr.problemas_zip(LIMPIO + ["Tenca/", "Tenca/_app/", "Tenca/_app/static/"], VERSIONADOS), [])
        malos = pr.problemas_zip(LIMPIO + ["Tenca/_app/server.py"], VERSIONADOS)
        self.assertEqual(len(malos), 1, malos)
        self.assertIn("repetido", malos[0])
        malos = pr.problemas_zip(LIMPIO + ["Tenca/Proyecto Alfa/"], VERSIONADOS)
        self.assertEqual(len(malos), 1, malos)
        self.assertIn("carpeta sin ficheros", malos[0])

    def test_faltan_ficheros_de_la_app(self):
        malos = pr.problemas_zip([n for n in LIMPIO if not n.endswith("/server.py")], VERSIONADOS)
        self.assertTrue(any("_app/server.py" in m for m in malos), malos)

    def test_sin_instalar_no_es_el_zip(self):
        malos = pr.problemas_zip([n for n in LIMPIO if not n.endswith("/INSTALAR.bat")], VERSIONADOS)
        self.assertTrue(any("INSTALAR.bat" in m for m in malos), malos)


@unittest.skipUnless(hay_git(), "sin git (copia descargada sin .git)")
class ZipRealTests(unittest.TestCase):
    """El zip de verdad, con el build_zip.py de esta versión exportada con git archive."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.carpeta = os.path.join(cls.tmp.name, "repo")
        os.makedirs(cls.carpeta)
        pr.exportar("HEAD", cls.carpeta)
        cls.versionados = pr.git("ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_el_zip_publico_pasa_la_comprobacion(self):
        dest = os.path.join(self.tmp.name, "publico.zip")
        pr.construir_zip(self.carpeta, dest, "2026-01-01 · abcdef0")
        nombres, malos = pr.comprobar_zip(dest, self.versionados)
        self.assertEqual(malos, [])
        self.assertIn("Tenca/INSTALAR.bat", nombres)
        txt = {n[len("Tenca/"):] for n in nombres if n.endswith(".txt")}
        self.assertTrue(txt <= pr.TXT_PERMITIDOS, txt)
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(z.read("Tenca/_app/version.txt").decode("utf-8").strip(), "2026-01-01 · abcdef0")

    def test_el_zip_con_listas_no_pasa(self):
        """El zip privado (build_zip.py --vocab) es justo lo que la comprobación tiene que parar."""
        lista = os.path.join(self.carpeta, "Proyecto Alfa", "Vocabulario")
        os.makedirs(lista, exist_ok=True)
        with open(os.path.join(lista, "personas.txt"), "w", encoding="utf-8") as f:
            f.write("Nombre Apellido\n")
        dest = os.path.join(self.tmp.name, "privado.zip")
        r = subprocess.run([sys.executable, os.path.join(self.carpeta, "scripts", "build_zip.py"), dest, "--vocab",
                            self.carpeta, "--version", "x"], cwd=self.carpeta, capture_output=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr)
        _, malos = pr.comprobar_zip(dest, self.versionados)
        self.assertTrue(any("Vocabulario" in m for m in malos), malos)

    def test_exportar_no_lleva_lo_ignorado(self):
        self.assertTrue(os.path.isfile(os.path.join(self.carpeta, "scripts", "build_zip.py")))
        for ignorado in (".git", "dist", ".venv"):
            self.assertFalse(os.path.exists(os.path.join(self.carpeta, ignorado)), ignorado)


CHANGELOG = """# Cambios

## Sin publicar

Intro.

### Voces dobles en la grabación (07/11/2026, msgs 1154 y 1156)

- Arreglado: una voz.

### Hoy (09/11/2026)

- Nuevo: Hoy.

### Seguridad

- Token.
"""

ANTERIOR = """# Cambios

## Sin publicar

### Seguridad

- Otro texto.

### Viejo que ya no está

- x.
"""


class NotasTests(unittest.TestCase):
    def test_tag(self):
        self.assertEqual(pr.nombre_tag("2026-11-14", "62f93ea"), "v2026.11.14-62f93ea")
        with self.assertRaises(pr.Fallo):
            pr.nombre_tag("14/11/2026", "62f93ea")

    def test_repo_github(self):
        for url in ("https://github.com/duena/app.git", "https://github.com/duena/app", "git@github.com:duena/app.git"):
            self.assertEqual(pr.repo_github(url), "duena/app")
        with self.assertRaises(pr.Fallo):
            pr.repo_github("https://gitlab.com/duena/app.git")

    def test_apartados_y_cambios(self):
        self.assertEqual([t for _, t, _ in pr.apartados(CHANGELOG)][1], "Hoy (09/11/2026)")
        nuevos, cambiados = pr.cambios(CHANGELOG, ANTERIOR)
        self.assertEqual([t for t, _ in nuevos], ["Voces dobles en la grabación (07/11/2026, msgs 1154 y 1156)",
                                                  "Hoy (09/11/2026)"])
        self.assertEqual([t for t, _ in cambiados], ["Seguridad"])
        nuevos, cambiados = pr.cambios(CHANGELOG, None)
        self.assertEqual(len(nuevos), 3)
        self.assertEqual(cambiados, [])

    def test_sin_referencias_internas(self):
        self.assertEqual(pr.sin_ref_interna("Voces dobles (07/11/2026, msgs 1154 y 1156)"), "Voces dobles (07/11/2026)")
        self.assertEqual(pr.sin_ref_interna("Grabar la siguiente (msg 1479)"), "Grabar la siguiente")
        self.assertEqual(pr.sin_ref_interna("Hoy (09/11/2026)"), "Hoy (09/11/2026)")

    def test_notas_primera_version(self):
        nuevos, _ = pr.cambios(CHANGELOG)
        t = pr.notas("2026-11-14 · 62f93ea", "f" * 40, "a" * 64, "duena/app", "v2026.11.14-62f93ea", nuevos, [],
                     primera=True)
        self.assertIn("a" * 64, t)
        self.assertIn("certutil -hashfile Tenca-instalador.zip SHA256", t)
        self.assertIn("- Voces dobles en la grabación (07/11/2026)\n", t)
        self.assertNotIn("1154", t)
        self.assertNotIn("Arreglado: una voz", t)  # la primera, solo el índice
        self.assertIn("https://github.com/duena/app/blob/v2026.11.14-62f93ea/CHANGELOG.md", t)

    def test_notas_con_anterior(self):
        nuevos, cambiados = pr.cambios(CHANGELOG.replace(", msgs 1154 y 1156", ""), ANTERIOR)
        t = pr.notas("v", "f" * 40, "a" * 64, "d/a", "v2026.11.14-62f93ea", nuevos, cambiados)
        self.assertIn("### Hoy (09/11/2026)\n\n- Nuevo: Hoy.", t)  # lo nuevo, entero
        self.assertIn("También cambian: «Seguridad».", t)
        # con una referencia interna en el texto, o demasiado largo, solo el índice
        nuevos, _ = pr.cambios(CHANGELOG.replace("- Nuevo: Hoy.", "- Nuevo (msg 1390)."), ANTERIOR)
        t = pr.notas("v", "f" * 40, "a" * 64, "d/a", "v2026.11.14-62f93ea", nuevos, [])
        self.assertNotIn("1390", t)
        self.assertIn("- Hoy (09/11/2026)\n", t)
        with mock.patch.object(pr, "LIMITE_NOTAS", 10):
            t = pr.notas("v", "f" * 40, "a" * 64, "d/a", "v2026.11.14-62f93ea", [("Hoy", "- Nuevo: Hoy.")], [])
        self.assertNotIn("- Nuevo: Hoy.", t)

    def test_orden_publicar(self):
        cmd = pr.orden_publicar("d/a", "v2026.11.14-62f93ea", "f" * 40, "2026-11-14 · 62f93ea", "z.zip", "z.sha256",
                                "n.md")
        self.assertEqual(cmd[:3], ["release", "create", "v2026.11.14-62f93ea"])
        self.assertIn("z.zip", cmd)
        self.assertIn("z.sha256", cmd)
        self.assertEqual(cmd[cmd.index("--target") + 1], "f" * 40)
        self.assertEqual(cmd[cmd.index("--repo") + 1], "d/a")


@unittest.skipUnless(hay_git(), "sin git")
class ReleaseAnteriorTests(unittest.TestCase):
    """Un repo de prueba: c0 - c1 - c2 en main y d, otra línea desde c0; Releases en c0 (anotado) y c1."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.invalid")

        def run(*a, fecha=None):
            e = dict(env, GIT_AUTHOR_DATE=fecha, GIT_COMMITTER_DATE=fecha) if fecha else env
            return subprocess.run(["git", "-C", tmp, *a], capture_output=True, text=True, env=e, timeout=30,
                                  check=True).stdout.strip()

        run("init", "-q", "-b", "main")
        self.c = []
        for i, fecha in enumerate(("2026-11-01T10:00:00", "2026-11-02T10:00:00", "2026-11-03T10:00:00")):
            run("commit", "-q", "--allow-empty", "-m", f"c{i}", fecha=fecha)
            self.c.append(run("rev-parse", "HEAD"))
        run("checkout", "-q", "-b", "otra", self.c[0])
        run("commit", "-q", "--allow-empty", "-m", "d", fecha="2026-11-04T10:00:00")
        self.d = run("rev-parse", "HEAD")
        run("checkout", "-q", "main")
        self.t0, self.t1 = f"v2026.11.01-{self.c[0][:7]}", f"v2026.11.02-{self.c[1][:7]}"
        run("tag", "-a", "-m", "anotado", self.t0, self.c[0])
        run("tag", self.t1, self.c[1])
        run("tag", "otro-tag", self.c[2])
        self.tags = pr.tags_release(tmp, cwd=tmp)

    def test_tags_release(self):
        self.assertEqual(self.tags, [(self.t0, self.c[0]), (self.t1, self.c[1])])  # el anotado, por su commit

    def test_ultimo_tag_antecesor(self):
        self.assertEqual(pr.release_anterior(self.c[2], self.tags, cwd=self.tmp), (self.t1, self.c[1]))
        self.assertEqual(pr.release_anterior(self.c[1], self.tags, cwd=self.tmp), (self.t0, self.c[0]))
        self.assertIsNone(pr.release_anterior(self.c[0], self.tags, cwd=self.tmp))

    def test_no_se_publica_una_version_anterior_a_la_ultima(self):
        """Publicar c0 o d con un Release en c1 los pondría como «Latest» y el README daría una versión vieja."""
        self.assertEqual(pr.no_antecesores(self.c[2], self.tags, cwd=self.tmp), [])
        self.assertEqual(pr.no_antecesores(self.c[1], self.tags, cwd=self.tmp), [])
        self.assertEqual(pr.no_antecesores(self.c[0], self.tags, cwd=self.tmp), [self.t1])
        self.assertEqual(pr.no_antecesores(self.d, self.tags, cwd=self.tmp), [self.t1])
        self.assertEqual(pr.no_antecesores(self.c[2], self.tags + [("v2026.11.09-abcdef0", "f" * 40)], cwd=self.tmp),
                         ["v2026.11.09-abcdef0"])  # un commit que no está en el clon no cuenta como antecesor

    def test_solo_se_publica_lo_que_esta_en_main(self):
        self.assertEqual(pr.resolver(self.c[1], base="main", cwd=self.tmp)[0], self.c[1])
        with self.assertRaises(pr.Fallo):
            pr.resolver(self.d, base="main", cwd=self.tmp)


if __name__ == "__main__":
    unittest.main()
