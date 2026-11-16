# -*- coding: utf-8 -*-
"""Publica una entrega como Release de GitHub: el zip PÚBLICO (sin listas de vocabulario) y su sha256.

    python scripts/publicar_release.py                     -> lo prepara y lo comprueba todo, sin publicar nada
    python scripts/publicar_release.py --publicar          -> además crea el Release de origin/main
    python scripts/publicar_release.py --ref <sha> ...     -> otra versión (tiene que estar ya en origin/main)

Pasos:
  1. Exporta los ficheros versionados de esa versión con `git archive` a una carpeta temporal: solo lo que está en git,
     nada de lo ignorado ni de lo que haya sin commitear en esta copia (listas de vocabulario, grabaciones, dist/).
  2. Genera el zip con el `scripts/build_zip.py` de esa misma versión, SIN `--vocab`, y la versión «AAAA-MM-DD · sha».
  3. Comprueba el zip: ninguna carpeta Vocabulario, ningún .txt fuera de los de la app y ningún fichero que no esté en
     git (salvo _app/version.txt, que lo escribe build_zip). Si algo falla, para y no publica.
  4. Calcula el sha256, escribe las notas (sha256, cómo comprobarlo y los apartados del CHANGELOG nuevos desde el
     Release anterior) y, con --publicar, `gh release create` con el tag vAAAA.MM.DD-<sha corto>.
  5. Descarga lo publicado y comprueba que su sha256 es el mismo.

El zip se sube como «Tenca-instalador.zip» (sin espacios: GitHub los cambia por puntos), así que el enlace
releases/latest/download/Tenca-instalador.zip del README siempre da el último. Necesita git, gh con sesión iniciada
y numpy (build_zip importa la app). El zip con listas de vocabulario es privado: este script no tiene forma de hacerlo.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

ASSET = "Tenca-instalador.zip"
RAIZ_ZIP = "Tenca"  # server.PKG_ROOT: la carpeta raíz dentro del zip
GENERADOS = {"_app/version.txt"}  # los escribe build_package; no están en git
# Los únicos .txt que lleva el zip público. Uno nuevo hace fallar la publicación hasta que se añada aquí: mejor parar
# una entrega que publicar una lista por error.
TXT_PERMITIDOS = {"LEEME.txt", "_app/export/LEEME.txt", "_app/version.txt", "_app/static/fuentes-OFL.txt"}
OBLIGATORIOS = {"INSTALAR.bat", "LEEME.txt", "_app/server.py", "_app/instalar.ps1", "_app/version.txt"}
TAG_RE = re.compile(r"^v\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7,40}$")
LIMITE_NOTAS = 60000  # GitHub admite 125.000 caracteres; con más, solo el índice de apartados
# «(07/11/2026, msgs N y M)» -> «(07/11/2026)»: referencias de trabajo que no dicen nada a quien descarga. El
# CHANGELOG ya no las lleva, pero el de un Release anterior puede llevarlas: cambios() compara sin ellas
REF_INTERNA_RE = re.compile(r",?\s*\bmsgs?\.?\s+\d+(?:\s*(?:,|y)\s*\d+)*", re.I)
HAY_REF_INTERNA_RE = re.compile(r"\bmsgs?\.?\s+\d{3,}", re.I)


class Fallo(Exception):
    """Algo impide publicar; el mensaje dice qué."""


def git(*args, cwd=REPO, binario=False, timeout=60):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise Fallo(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if binario else r.stdout.decode("utf-8").strip()


def git_ok(*args, cwd=REPO):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=60).returncode == 0


# ----------------------------------------------------------------------------- versión y tag

def nombre_tag(fecha, corto):
    """«2026-11-14», «62f93ea» -> «v2026.11.14-62f93ea»."""
    tag = f"v{fecha.replace('-', '.')}-{corto}"
    if not TAG_RE.match(tag):
        raise Fallo(f"tag con forma inesperada: {tag}")
    return tag


def resolver(ref, base="origin/main", cwd=REPO):
    """(sha completo, sha corto, fecha del commit) de `ref`, que tiene que estar en `base`: no se publica una rama."""
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd)
    if not git_ok("merge-base", "--is-ancestor", sha, base, cwd=cwd):
        raise Fallo(f"{ref} ({sha[:12]}) no está en {base}: primero se integra y luego se publica")
    return sha, git("rev-parse", "--short", sha, cwd=cwd), git("log", "-1", "--format=%cs", sha, cwd=cwd)


def repo_github(url):
    """«https://github.com/dueño/repo.git» o «git@github.com:dueño/repo.git» -> «dueño/repo»."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$",
                 url.strip())
    if not m:
        raise Fallo(f"el remoto no es de GitHub: {url}")
    return m.group(1)


# ----------------------------------------------------------------------------- zip público

def exportar(sha, destino, cwd=REPO):
    """Los ficheros versionados de `sha` en `destino` (git archive): nada ignorado ni sin commitear."""
    tar = os.path.join(os.path.dirname(os.path.abspath(destino)), "export.tar")
    git("archive", "--format=tar", "-o", tar, sha, cwd=cwd, timeout=300)
    with tarfile.open(tar) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(destino, filter="data")
        else:  # Python sin el filtro (3.10 viejo): git archive no lleva rutas absolutas ni «..», pero se comprueba
            for m in t.getmembers():
                if m.name.startswith(("/", "\\")) or ".." in m.name.replace("\\", "/").split("/"):
                    raise Fallo(f"ruta peligrosa en git archive: {m.name}")
            t.extractall(destino)
    os.remove(tar)


def construir_zip(carpeta, dest, version, python=sys.executable):
    """El build_zip.py de la versión exportada, sin --vocab."""
    cmd = [python, os.path.join(carpeta, "scripts", "build_zip.py"), dest, "--version", version]
    r = subprocess.run(cmd, cwd=carpeta, capture_output=True, timeout=600)
    salida = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0 or not os.path.isfile(dest):
        raise Fallo(f"build_zip.py falló (rc {r.returncode}): {salida[-2000:]}")
    return salida


def problemas_zip(nombres, versionados):
    """Lo que impide publicar un zip con estas entradas. `versionados`: las rutas que hay en git en esa versión.
    El zip público lleva solo ficheros de git (con el layout de build_package) y _app/version.txt."""
    out, aceptados, carpetas, vistos = [], set(), [], set()
    versionados = set(versionados)
    plantillas = {p.split("/")[-1] for p in versionados if p.startswith("_app/export/") and p.count("/") == 2}
    for n in nombres:
        partes = n.replace("\\", "/").split("/")
        clave = "/".join(partes).casefold()
        if clave in vistos:
            out.append(f"{n}: repetido")
            continue
        vistos.add(clave)
        if n.startswith(("/", "\\")) or ".." in partes or re.match(r"^[A-Za-z]:", n):
            out.append(f"{n}: ruta absoluta o con «..»")
            continue
        if any(p.casefold() == "vocabulario" for p in partes):
            out.append(f"{n}: carpeta Vocabulario (las listas solo van en un zip privado)")
            continue
        if partes[0] != RAIZ_ZIP or len(partes) < 2:
            out.append(f"{n}: fuera de la carpeta {RAIZ_ZIP}/")
            continue
        rel = "/".join(partes[1:])
        if not rel or n.endswith("/"):
            carpetas.append((n, rel))  # una carpeta vale solo si lleva algún fichero aceptado (abajo)
            continue
        if rel.lower().endswith(".txt") and rel not in TXT_PERMITIDOS:
            out.append(f"{n}: .txt que no es de la app (si lo es, añádelo a TXT_PERMITIDOS)")
            continue
        if rel in GENERADOS or ("/" not in rel and (rel in plantillas or (rel == "LICENSE" and rel in versionados))) \
                or (rel.startswith("_app/") and rel in versionados):
            aceptados.add(rel)  # en la raíz, solo las plantillas de _app/export y la licencia
            continue
        out.append(f"{n}: no está en git en esta versión, o no es de lo que lleva el zip")
    for n, rel in carpetas:
        if rel and not any(a.startswith(rel.rstrip("/") + "/") for a in aceptados):
            out.append(f"{n}: carpeta sin ficheros de la app")
    for rel in sorted(OBLIGATORIOS - aceptados):
        out.append(f"falta {RAIZ_ZIP}/{rel}: no parece el zip instalable")
    return out


def comprobar_zip(path, versionados):
    import zipfile
    with zipfile.ZipFile(path) as z:
        nombres = z.namelist()
        roto = z.testzip()
    out = problemas_zip(nombres, versionados)
    if roto:
        out.append(f"{roto}: CRC incorrecto")
    return nombres, out


def sha256_de(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


# ----------------------------------------------------------------------------- notas

def apartados(changelog):
    """[(sección, título, cuerpo)] de los apartados «### » del CHANGELOG, en orden; sección es el «## » del que
    cuelgan."""
    out, seccion, titulo, cuerpo = [], None, None, []

    def cerrar():
        if titulo is not None:
            out.append((seccion, titulo, "\n".join(cuerpo).strip()))

    for linea in changelog.replace("\r\n", "\n").split("\n"):
        if linea.startswith("## "):
            cerrar()
            seccion, titulo, cuerpo = linea[3:].strip(), None, []
        elif linea.startswith("### "):
            cerrar()
            titulo, cuerpo = linea[4:].strip(), []
        elif titulo is not None:
            cuerpo.append(linea)
    cerrar()
    return out


def sin_ref_interna(texto):
    # con el blanco de delante: «una voz (msg N).» -> «una voz.», no «una voz .»
    return re.sub(r"\s*\(\s*\)", "", REF_INTERNA_RE.sub("", texto)).strip()


def cambios(changelog, anterior=None):
    """(nuevos, cambiados): apartados del primer «## » (el de lo aún sin publicar) que no estaban en el CHANGELOG del
    Release anterior, y los que estaban con otro texto (título y texto comparados sin referencias internas). Sin
    Release anterior, todos son nuevos."""
    todos = apartados(changelog)
    if not todos:
        return [], []
    primera = todos[0][0]
    actuales = [(t, c) for s, t, c in todos if s == primera]
    if anterior is None:
        return actuales, []
    # sin las referencias internas: un apartado que solo ha perdido su «(msgs N y M)» no es nuevo ni ha cambiado
    antes = {sin_ref_interna(t): sin_ref_interna(c) for _, t, c in apartados(anterior)}
    nuevos = [(t, c) for t, c in actuales if sin_ref_interna(t) not in antes]
    cambiados = [(t, c) for t, c in actuales
                 if sin_ref_interna(t) in antes and antes[sin_ref_interna(t)] != sin_ref_interna(c)]
    return nuevos, cambiados


def notas(version, sha, sha256, repo, tag, nuevos, cambiados, primera=False):
    lineas = [
        f"Tenca {version} (commit `{sha}`).",
        "",
        f"**Descargar:** `{ASSET}`, abajo en «Assets». Es el zip público: no lleva listas de vocabulario. Se "
        "instala como dice el LEEME que va dentro (descomprimir y doble clic en `INSTALAR.bat`).",
        "",
        f"sha256 de `{ASSET}`:",
        "",
        "```",
        sha256,
        "```",
        "",
        f"Para comprobarlo en Windows, en la carpeta donde lo descargaste: `certutil -hashfile {ASSET} SHA256` "
        "tiene que dar esa misma línea (en PowerShell, `Get-FileHash .\\" + ASSET + "` la da en mayúsculas: "
        "es la misma).",
        "",
        "## Cambios",
        "",
    ]
    cuerpo = "\n\n".join(f"### {sin_ref_interna(t)}\n\n{c}" for t, c in nuevos)
    if primera:
        lineas.append("Primera versión publicada como Release. Lo que trae, por apartados del CHANGELOG:")
        lineas.append("")
        lineas += [f"- {sin_ref_interna(t)}" for t, _ in nuevos]
    elif not nuevos and not cambiados:
        lineas.append("Sin apartados nuevos en el CHANGELOG desde el Release anterior.")
    elif nuevos and (len(cuerpo) > LIMITE_NOTAS or HAY_REF_INTERNA_RE.search(cuerpo)):
        lineas += [f"- {sin_ref_interna(t)}" for t, _ in nuevos]
    elif nuevos:
        lineas.append(cuerpo)
    if cambiados:
        lineas += ["", "También cambian: " + "; ".join(f"«{sin_ref_interna(t)}»" for t, _ in cambiados) + "."]
    lineas += ["", f"Detalle: [CHANGELOG.md de esta versión](https://github.com/{repo}/blob/{tag}/CHANGELOG.md)."]
    return "\n".join(lineas) + "\n"


def tags_release(remoto="origin", cwd=REPO):
    """[(tag, commit)] de los tags vAAAA.MM.DD-… del remoto (los anotados, por su commit)."""
    directos, pelados = {}, {}
    for linea in git("ls-remote", "--tags", remoto, cwd=cwd).splitlines():
        objeto, ref = linea.split("\t")
        nombre = ref[len("refs/tags/"):]
        if nombre.endswith("^{}"):
            pelados[nombre[:-3]] = objeto
        else:
            directos[nombre] = objeto
    return sorted((n, pelados.get(n, o)) for n, o in directos.items() if TAG_RE.match(n))


def release_anterior(sha, tags, cwd=REPO):
    """(tag, commit) del último de `tags` que es antecesor de `sha` (por fecha del commit), o None."""
    candidatos = []
    for nombre, commit in tags:
        if commit != sha and git_ok("merge-base", "--is-ancestor", commit, sha, cwd=cwd):
            candidatos.append((int(git("log", "-1", "--format=%ct", commit, cwd=cwd)), nombre, commit))
    return max(candidatos)[1:] if candidatos else None


def no_antecesores(sha, tags, cwd=REPO):
    """Los de `tags` que no son antecesores de `sha`: un Release más nuevo (o de otra línea). Con alguno, esta versión
    no se publica: saldría como «Latest» y el enlace del README daría la vieja. Un commit que no está en este clon
    tampoco cuenta como antecesor."""
    return [n for n, c in tags if c != sha and not git_ok("merge-base", "--is-ancestor", c, sha, cwd=cwd)]


def comprobar_publicable(sha, version, tags, cwd=REPO):
    """Para (Fallo) si `sha` ya está publicado con cualquier tag, o si hay un Release posterior o de otra línea."""
    ya = [n for n, c in tags if c == sha]
    if ya:
        raise Fallo(f"{sha[:12]} ya está publicado como {ya[0]}")
    posteriores = no_antecesores(sha, tags, cwd=cwd)
    if posteriores:
        raise Fallo(f"ya hay un Release posterior o de otra línea ({', '.join(posteriores)}): {version} no se publica, "
                    "porque saldría como el último")


# ----------------------------------------------------------------------------- publicar

def gh(*args, timeout=600):
    r = subprocess.run(["gh", *args], capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise Fallo(f"gh {' '.join(args[:3])}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace").strip()


def orden_publicar(repo, tag, sha, version, zip_path, sha_path, notas_path):
    return ["release", "create", tag, zip_path, sha_path, "--repo", repo, "--target", sha,
            "--title", f"Tenca {version}", "--notes-file", notas_path, "--latest"]


def main(argv):
    try:  # en Windows, con la salida redirigida, stdout va en cp1252 y «versión» rompe a quien la lea como UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Publica el zip público como Release de GitHub.")
    ap.add_argument("--ref", default="origin/main", help="versión a publicar (por defecto, origin/main)")
    ap.add_argument("--publicar", action="store_true", help="crear el Release (sin esto, solo prepara y comprueba)")
    ap.add_argument("--salida", default=os.path.join(REPO, "dist", "release"), help="carpeta del zip y las notas")
    a = ap.parse_args(argv[1:])
    try:
        git("fetch", "--quiet", "origin", "main", timeout=120)
        sha, corto, fecha = resolver(a.ref)
        version, tag = f"{fecha} · {corto}", nombre_tag(fecha, corto)
        repo = repo_github(git("remote", "get-url", "origin"))
        if git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
            raise Fallo(f"el tag {tag} ya existe en origin: esa versión ya está publicada")
        tags = tags_release()
        comprobar_publicable(sha, version, tags)
        os.makedirs(a.salida, exist_ok=True)
        zip_path = os.path.join(a.salida, ASSET)
        with tempfile.TemporaryDirectory(prefix="tenca-release-") as tmp:
            carpeta = os.path.join(tmp, "repo")
            os.makedirs(carpeta)
            exportar(sha, carpeta)
            print(construir_zip(carpeta, zip_path, version))
        versionados = git("ls-tree", "-r", "--name-only", "-z", sha).split("\0")
        nombres, malos = comprobar_zip(zip_path, versionados)
        if malos:
            raise Fallo("el zip no se puede publicar:\n  " + "\n  ".join(malos))
        txt = sorted(n for n in nombres if n.lower().endswith(".txt"))
        print(f"zip comprobado: {len(nombres)} entradas, sin carpetas Vocabulario; .txt: {', '.join(txt)}")
        suma = sha256_de(zip_path)
        sha_path = zip_path + ".sha256"
        with open(sha_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"{suma}  {ASSET}\n")
        previo = release_anterior(sha, tags)
        anterior = git("show", f"{previo[1]}:CHANGELOG.md") if previo else None
        nuevos, cambiados = cambios(git("show", f"{sha}:CHANGELOG.md"), anterior)
        texto = notas(version, sha, suma, repo, tag, nuevos, cambiados, primera=previo is None)
        notas_path = os.path.join(a.salida, "notas.md")
        with open(notas_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(texto)
        print(f"versión {version} · tag {tag} · anterior {previo[0] if previo else '(ninguno)'}")
        print(f"sha256 {suma}  {zip_path}")
        print(f"notas: {notas_path}")
        if not a.publicar:
            print("No se ha publicado nada. Para publicar: python scripts/publicar_release.py --publicar"
                  + ("" if a.ref == "origin/main" else f" --ref {a.ref}"))
            return 0
        print(gh(*orden_publicar(repo, tag, sha, version, zip_path, sha_path, notas_path)))
        with tempfile.TemporaryDirectory(prefix="tenca-release-check-") as tmp:
            gh("release", "download", tag, "--repo", repo, "--pattern", ASSET, "--dir", tmp)
            bajado = sha256_de(os.path.join(tmp, ASSET))
        if bajado != suma:
            raise Fallo(f"el zip publicado tiene otro sha256 ({bajado}): revisa el Release {tag}")
        print(f"publicado y comprobado: https://github.com/{repo}/releases/tag/{tag} (sha256 igual al descargarlo)")
        return 0
    except (Fallo, subprocess.TimeoutExpired, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
