#!/usr/bin/env python3
"""tools/build_portable.py - build the "no Python installed" CRT-MEDIA bundle.

    <proj>/.venv/Scripts/python.exe tools/build_portable.py

Output (both re-runnable - the script wipes and rebuilds them):

    dist/CRT-MEDIA-<ver>-portable/       the bundle, runnable in place
    dist/CRT-MEDIA-<ver>-portable.zip    the distributable (prints size + sha256)

``<ver>`` is read from the VERSION file at the project root - the single place
the version lives, so nothing here has to be edited for a release.  Pass
``--version X.Y.Z`` to override it for a one-off build.

The bundle carries its own copy of the CPython *embeddable* runtime plus every
wheel the app needs installed into it, so the machine it is copied to needs no
Python, no pip and no compiler.

What it does, in order
    1. downloads (and caches) python-<v>-embed-amd64.zip from python.org
    2. extracts it into <bundle>/python
    3. repairs the runtime's ``pythonXY._pth`` so the embeddable interpreter
       can see ``Lib\\site-packages`` and the bundle root, with ``import site``
       enabled - the embeddable distribution ships no pip and an isolated
       sys.path, which is why this step exists
    4. installs requirements.txt into <bundle>/python/Lib/site-packages with
       uv, wheels only (``--only-binary :all:``), so a dependency that would
       need a C compiler fails the build instead of half-working
    5. copies the app files and web/ (no dev/, no CONTRACT.md, no .venv)
    6. writes the CRT-MEDIA.vbs / CRT-MEDIA.bat launchers and FIRST-RUN.txt
    7. VERIFIES the finished bundle by running its own python: every runtime
       import, then a real MediaController.get_state() against the live
       Windows media API - the build fails and leaves the tree for inspection
       rather than shipping something broken
    8. prunes __pycache__ and zips the result

Only the standard library is used on the build side; uv must be on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# what goes in
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"


def read_version(override: str = None) -> str:
    """The app version - from VERSION, or from --version for a one-off build.

    The version lives in exactly one file so a release cannot ship a folder,
    a zip and a README that disagree with each other.
    """
    if override:
        version, source = override.strip(), "--version"
    else:
        source = VERSION_FILE.name
        try:
            version = VERSION_FILE.read_text(encoding="ascii").strip()
        except UnicodeDecodeError as exc:
            die("%s is not plain ASCII: %s" % (VERSION_FILE, exc))
        except OSError as exc:
            die("cannot read %s: %s" % (VERSION_FILE, exc))
    if not version:
        die("%s is empty - it must hold the version, e.g. 1.0.1" % source)
    allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "abcdefghijklmnopqrstuvwxyz.-+_")
    if any(ch not in allowed for ch in version):
        die("%s holds %r, which is not a usable version string"
            % (source, version))
    return version


def bundle_name(version: str) -> str:
    return "CRT-MEDIA-%s-portable" % version


EMBED_URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
# preferred version first, then the rest of the 3.14 line (the app is developed
# and frozen against 3.14.x; a patch-release difference is harmless)
VERSION_CANDIDATES = ["3.14.7", "3.14.6", "3.14.5", "3.14.4", "3.14.3", "3.14.2",
                      "3.14.1", "3.14.0"]

APP_FILES = ["app.py", "media.py", "media_selftest.py"]
APP_DIRS = ["web"]                       # includes web/fonts/
DOC_FILES = ["README.md", "requirements.txt", "VERSION"]
FIRST_RUN_SRC = ROOT / "tools" / "PORTABLE-FIRST-RUN.txt"
REQUIREMENTS = ROOT / "requirements.txt"

# proxy-tools 0.1.0 is published as an sdist only. It is a single pure-Python
# module (proxy_tools/__init__.py, pulled in by pywebview) so it needs no
# compiler; it is the one package allowed to be built from source. Every other
# requirement is installed wheel-only, and a new source-only dependency fails
# the build loudly instead of silently producing a bundle that needs MSVC.
PURE_PYTHON_SDIST = ["proxy-tools"]

# things that must never reach the bundle
EXCLUDE_NAMES = ("dev", ".venv", "CONTRACT.md", ".git")
EXCLUDE_SUFFIXES = (".pyc", ".pyo")

VBS_LAUNCHER = """' CRT-MEDIA // portable silent launcher.
' Requires nothing installed: the widget runs on the private CPython copy in
' the "python" folder next to this file.
Option Explicit

Dim fso, shell, here, pyw, app, q
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
q = Chr(34)

here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = here & "\\python\\pythonw.exe"
app = here & "\\app.py"

If Not fso.FileExists(app) Then
  MsgBox "CRT-MEDIA: app.py is missing from:" & vbCrLf & vbCrLf & here & _
         vbCrLf & vbCrLf & "This folder looks incomplete - extract the whole " & _
         "CRT-MEDIA zip again, keeping the folder structure.", 16, "CRT-MEDIA"
  WScript.Quit 2
End If

If Not fso.FileExists(pyw) Then
  MsgBox "CRT-MEDIA: the bundled Python runtime is missing:" & vbCrLf & vbCrLf & _
         pyw & vbCrLf & vbCrLf & "This folder looks incomplete - extract the " & _
         "whole CRT-MEDIA zip again, keeping the folder structure." & vbCrLf & _
         "If you built this from source, re-run tools\\build_portable.py.", 16, "CRT-MEDIA"
  WScript.Quit 2
End If

' run from the bundle root so relative paths resolve, hidden window, no wait
shell.CurrentDirectory = here
shell.Run q & pyw & q & " " & q & app & q, 0, False
WScript.Quit 0
"""

BAT_LAUNCHER = """@echo off
rem CRT-MEDIA // portable troubleshooting launcher: keeps a console open with
rem the widget's log. For the normal, silent start use CRT-MEDIA.vbs.
setlocal
set "HERE=%~dp0"
set "PY=%HERE%python\\python.exe"
set "APP=%HERE%app.py"

if not exist "%APP%" goto :noapp
if not exist "%PY%" goto :noruntime

echo [CRT-MEDIA] starting the widget in debug mode (close this window to be
echo [CRT-MEDIA] sure you have the log if it misbehaves).
echo.
"%PY%" "%APP%" --debug
set "RC=%errorlevel%"
echo.
echo [CRT-MEDIA] the widget exited with code %RC%.
pause
exit /b %RC%

:noapp
echo [CRT-MEDIA] ERROR: app.py is missing from:
echo             %HERE%
echo             This folder looks incomplete - extract the whole CRT-MEDIA
echo             zip again, keeping the folder structure.
pause
exit /b 2

:noruntime
echo [CRT-MEDIA] ERROR: the bundled Python runtime is missing:
echo             %PY%
echo             This folder looks incomplete - extract the whole CRT-MEDIA
echo             zip again, keeping the folder structure.
pause
exit /b 2
"""

# Runs inside the finished bundle with the bundled interpreter.
VERIFY_PROBE = r'''
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODULES = ["webview", "winrt.windows.media.control", "pycaw", "pystray", "PIL", "media"]
report = {"exe": sys.executable, "version": sys.version.split()[0],
          "prefix": sys.prefix, "modules": {}, "media": None}

for name in MODULES:
    try:
        __import__(name)
        report["modules"][name] = "ok"
    except Exception as exc:
        report["modules"][name] = "%s: %s" % (type(exc).__name__, exc)

try:
    import media
    ctl = media.MediaController()
    st = ctl.get_state()
    report["media"] = {
        "ok": bool(st.get("ok")),
        "has_session": bool(st.get("has_session")),
        "app_name": st.get("app_name"),
        "status": st.get("status"),
        "volume": st.get("volume"),
        "muted": st.get("muted"),
        "exact_contract_keys": sorted(st.keys()) == sorted(media.STATE_KEYS),
        "error": st.get("error"),
    }
    ctl.shutdown()
except Exception:
    report["media"] = {"error": traceback.format_exc()}

print(json.dumps(report, indent=2))

bad = [m for m, v in report["modules"].items() if v != "ok"]
mrec = report["media"] or {}
if bad or mrec.get("error") is not None or not mrec.get("ok"):
    raise SystemExit(1)
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def step(msg: str) -> None:
    print("[build] %s" % msg, flush=True)


def die(msg: str, code: int = 1):
    print("\n[build] FATAL: %s" % msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    printable = " ".join(str(c) for c in cmd)
    step("$ %s" % printable)
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)
    if proc.stdout and proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        if proc.stderr and proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)
        die("command failed (exit %d): %s" % (proc.returncode, printable))
    return proc


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%d B" % n


def url_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def resolve_python_version(requested: str, allowed: list[str]) -> str:
    """Pick the newest usable 3.14.x embeddable build that actually exists."""
    order = [requested] + [v for v in VERSION_CANDIDATES if v != requested]
    step("probing python.org for the embeddable amd64 build ...")
    for v in order:
        url = EMBED_URL.format(v=v)
        if url_ok(url):
            if v != requested:
                step("NOTE: %s is not published; using the closest 3.14.x: %s"
                     % (requested, v))
            step("using CPython %s  ->  %s" % (v, url))
            return v
    die("no CPython 3.14.x embeddable amd64 build found on python.org "
        "(tried %s). Check the network/proxy, or pass --python-version."
        % ", ".join(order))


def fetch_embed_zip(version: str, cache_dir: Path) -> Path:
    url = EMBED_URL.format(v=version)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / ("python-%s-embed-amd64.zip" % version)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        step("using cached %s (%s)" % (dest.name, human(dest.stat().st_size)))
        if zipfile.is_zipfile(dest):
            return dest
        step("cached download is not a valid zip - refetching")
        dest.unlink()
    step("downloading %s" % url)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh, 1 << 20)
    except (urllib.error.URLError, OSError) as exc:
        if dest.exists():
            dest.unlink()
        die("download failed: %s" % exc)
    if not zipfile.is_zipfile(dest):
        dest.unlink()
        die("downloaded file is not a zip: %s" % url)
    step("downloaded %s (%s)" % (dest.name, human(dest.stat().st_size)))
    return dest


def extract_runtime(zip_path: Path, runtime_dir: Path) -> None:
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)
    step("extracting the embeddable runtime into %s" % runtime_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(runtime_dir)
    if not (runtime_dir / "python.exe").is_file():
        die("extracted runtime has no python.exe - unexpected embeddable layout")


def patch_pth(runtime_dir: Path) -> Path:
    """Make the embeddable interpreter find site-packages and the bundle root.

    A ._pth file puts CPython into isolated mode: sys.path comes from that file
    alone (the script's own folder is NOT added) and site-packages is not
    imported. Both of those have to be spelled out here.
    """
    candidates = sorted(runtime_dir.glob("python*._pth"))
    if len(candidates) != 1:
        die("expected exactly one pythonXY._pth in %s, found %d: %s"
            % (runtime_dir, len(candidates), [p.name for p in candidates]))
    pth = candidates[0]
    original = pth.read_text(encoding="utf-8", errors="replace")
    stage0 = [ln.strip() for ln in original.splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    if not stage0:
        die("cannot read the stdlib zip entry out of %s:\n%s" % (pth.name, original))
    stdlib_zip = stage0[0]
    if not (runtime_dir / stdlib_zip).is_file():
        die("%s names %r, which is not in the runtime folder" % (pth.name, stdlib_zip))

    body = [
        "# CRT-MEDIA portable runtime - paths are relative to this folder.",
        stdlib_zip,
        ".",
        "Lib\\site-packages",      # the installed wheels
        "..",                      # the bundle root, so `import media` works
        "",
        "import site",             # required: the embeddable ships no pip/site
        "",
    ]
    pth.write_text("\r\n".join(body), encoding="utf-8", newline="")
    step("patched %s -> %s" % (pth.name, " | ".join(body[:6])))
    return pth


def install_wheels(uv: str, runtime_py: Path, site_packages: Path) -> None:
    site_packages.mkdir(parents=True, exist_ok=True)
    cmd = [uv, "pip", "install",
           "--target", str(site_packages),
           "--python", str(runtime_py),
           "--only-binary", ":all:"]
    for name in PURE_PYTHON_SDIST:
        cmd += ["--no-binary", name]
    cmd += ["-r", str(REQUIREMENTS)]
    step("installing requirements.txt as wheels (exceptions: %s)"
         % (", ".join(PURE_PYTHON_SDIST) or "none"))
    run(cmd)
    missing = [n for n in ("webview", "winrt", "pycaw", "pystray", "PIL", "psutil")
               if not (site_packages / n).exists()]
    if missing:
        die("site-packages is missing %s after the install" % missing)
    step("site-packages: %d entries, %s" % (
        len(list(site_packages.iterdir())),
        human(sum(f.stat().st_size for f in site_packages.rglob("*") if f.is_file()))))


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def assemble(bundle: Path) -> None:
    for name in APP_FILES:
        src = ROOT / name
        if not src.is_file():
            die("required app file is missing: %s" % src)
        shutil.copy2(src, bundle / name)
    for name in APP_DIRS:
        src = ROOT / name
        if not src.is_dir():
            die("required app folder is missing: %s" % src)
        copy_tree(src, bundle / name)
    for name in DOC_FILES:
        src = ROOT / name
        if not src.is_file():
            die("required doc file is missing: %s" % src)
        shutil.copy2(src, bundle / name)
    if not FIRST_RUN_SRC.is_file():
        die("first-run note is missing: %s" % FIRST_RUN_SRC)
    shutil.copy2(FIRST_RUN_SRC, bundle / "FIRST-RUN.txt")
    step("copied app files + %s + %s" % (", ".join(APP_DIRS), ", ".join(DOC_FILES)))

    # every excluded thing must be provably absent
    for pattern in EXCLUDE_NAMES:
        hits = [p for p in bundle.rglob(pattern)]
        if hits:
            die("excluded path leaked into the bundle: %s" % hits)

    (bundle / "CRT-MEDIA.vbs").write_text(VBS_LAUNCHER, encoding="ascii", newline="")
    (bundle / "CRT-MEDIA.bat").write_text(BAT_LAUNCHER, encoding="ascii", newline="")
    step("wrote CRT-MEDIA.vbs (silent) and CRT-MEDIA.bat (visible console)")


def verify_bundle(bundle: Path) -> dict:
    """Run the bundled interpreter against its own installed wheels."""
    py = bundle / "python" / "python.exe"
    probe = bundle / "_verify_probe.py"
    probe.write_text(VERIFY_PROBE, encoding="utf-8")
    step("verifying the bundle with its own interpreter ...")
    try:
        proc = subprocess.run([str(py), str(probe)], cwd=str(bundle),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
    finally:
        probe.unlink()

    print(proc.stdout.rstrip() if proc.stdout and proc.stdout.strip() else "(no stdout)")
    if proc.returncode != 0:
        if proc.stderr and proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)
        die("the freshly built bundle FAILED its own verification (exit %d). "
            "The broken tree is left at %s for inspection - nothing was zipped."
            % (proc.returncode, bundle))
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        die("could not parse the verification report")
    if report.get("exe", "").lower() != str(py).lower():
        die("the probe ran on %s, not the bundled interpreter" % report.get("exe"))
    step("verification passed on %s" % report["exe"])
    return report


def prune(bundle: Path) -> None:
    removed = 0
    for path in sorted(bundle.rglob("*"), reverse=True):
        if path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        elif path.is_file() and path.suffix in EXCLUDE_SUFFIXES:
            path.unlink()
            removed += 1
    step("pruned %d cache artefact(s) (__pycache__ / *.pyc)" % removed)


def make_zip(bundle: Path, zip_path: Path, name: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    step("zipping -> %s" % zip_path)
    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            zf.write(p, "%s/%s" % (name, p.relative_to(bundle).as_posix()))
    step("zipped %d files" % len(files))


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python-version", default=VERSION_CANDIDATES[0],
                    help="CPython 3.14.x embeddable build to bundle "
                         "(default: %(default)s)")
    ap.add_argument("--version", default=None, metavar="X.Y.Z",
                    help="version to stamp on the bundle folder and the zip "
                         "name (default: the version in the VERSION file, "
                         "e.g. 1.0.1)")
    args = ap.parse_args(argv)

    app_version = read_version(args.version)
    name = bundle_name(app_version)
    dist = ROOT / "dist"
    bundle = dist / name
    zip_path = dist / ("%s.zip" % name)
    cache_dir = dist / ".cache"

    step("project root   %s" % ROOT)
    step("app version    %s (from %s)" % (
        app_version, "--version" if args.version else VERSION_FILE.name))
    step("bundle         %s" % bundle)

    if not REQUIREMENTS.is_file():
        die("requirements.txt is missing from %s" % ROOT)
    uv = shutil.which("uv")
    if not uv:
        die("uv is not on PATH. The embeddable runtime has no pip, so the "
            "wheels have to be installed with uv:\n"
            "    winget install astral-sh.uv    (or see https://docs.astral.sh/uv/)")
    step("uv             %s" % uv)

    version = resolve_python_version(args.python_version, VERSION_CANDIDATES)
    embed_zip = fetch_embed_zip(version, cache_dir)

    if bundle.exists():
        step("removing the previous %s" % bundle.name)
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    runtime_dir = bundle / "python"
    extract_runtime(embed_zip, runtime_dir)
    patch_pth(runtime_dir)
    install_wheels(uv, runtime_dir / "python.exe", runtime_dir / "Lib" / "site-packages")
    assemble(bundle)
    report = verify_bundle(bundle)
    prune(bundle)
    make_zip(bundle, zip_path, name)

    size = zip_path.stat().st_size
    digest = sha256_of(zip_path)

    print()
    print("=" * 72)
    print("PORTABLE BUNDLE READY")
    print("  version      %s  (bundled CPython %s)" % (app_version, version))
    print("  bundle dir   %s" % bundle)
    print("  zip          %s" % zip_path)
    print("  zip size     %s (%d bytes)" % (human(size), size))
    print("  zip sha256   %s" % digest)
    print("  interpreter  %s" % report["exe"])
    print("  media probe  has_session=%s app=%s status=%s volume=%s"
          % (report["media"].get("has_session"), report["media"].get("app_name"),
             report["media"].get("status"), report["media"].get("volume")))
    print("  start with   CRT-MEDIA.vbs   (or CRT-MEDIA.bat to see the log)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
