"""Assemble a portable agent build and corresponding source archive without changing the reference installation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.17"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(*args) -> bytes:
    return subprocess.check_output(["git", "-c", f"safe.directory={ROOT.as_posix()}", *args], cwd=ROOT)


def included(path: Path, base: Path) -> bool:
    relative = path.relative_to(base)
    # Never archive user programs added to the local controlled directory.
    if relative.parts[0].casefold() == "dosdrive" and relative.as_posix().casefold() != "dosdrive/readme.txt":
        return False
    return path.is_file() and not any(part in (".runtime", "__pycache__") for part in path.relative_to(base).parts) and path.suffix != ".pyc"


def finalize(stage: Path):
    print("Recording hashes and creating portable ZIP...", flush=True)
    files = sorted(path for path in stage.rglob("*") if included(path, stage) and path.name != "SHA256SUMS.txt")
    (stage / "SHA256SUMS.txt").write_text("".join(f"{sha256(path)}  {path.relative_to(stage).as_posix()}\n" for path in files), encoding="utf-8")
    archive = stage.parent / f"{stage.name}.zip"
    if archive.exists():
        raise FileExistsError(archive)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(stage.rglob("*")):
            if included(path, stage):
                bundle.write(path, f"{stage.name}/{path.relative_to(stage).as_posix()}")
    archive.with_suffix(".zip.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")

    print("Creating corresponding source ZIP...", flush=True)
    tracked = git("ls-files", "-z").decode("utf-8").split("\0")
    extra = git("ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
    extra = [name for name in extra if name.startswith(("client/python/", "docs/", "scripts/", "packaging/", "tests/agent/"))
             and Path(name).suffix in (".py", ".md", ".ps1", ".cmd", ".toml", ".lock", ".env", ".txt")]
    source_archive = stage.parent / f"{stage.name}-source.zip"
    if source_archive.exists():
        raise FileExistsError(source_archive)
    with zipfile.ZipFile(source_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for name in sorted(set(tracked + extra)):
            path = ROOT / name
            if name and not name.casefold().startswith("dosdrive/") and path.is_file():
                bundle.write(path, f"{stage.name}-source/{name}")
        bundle.write(stage / "BUILD-INFO.json", f"{stage.name}-source/BUILD-INFO.json")
    source_archive.with_suffix(".zip.sha256").write_text(f"{sha256(source_archive)}  {source_archive.name}\n", encoding="utf-8")
    print(f"Portable ZIP: {archive} ({archive.stat().st_size:,} bytes)")
    print(f"Source ZIP: {source_archive} ({source_archive.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, help="DOSBox-X runtime asset directory; required when staging.")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/releases/DOSBox-X-MCP-0.2.0-win64")
    parser.add_argument("--stage-only", action="store_true", help="Prepare the folder for verification before creating archives.")
    parser.add_argument("--archive-only", action="store_true", help="Archive a previously staged and verified folder.")
    args = parser.parse_args()
    stage = args.output.resolve()
    # All writes stay in the workspace. Never delete/reuse an existing build folder.
    if not stage.is_relative_to(ROOT) or stage == ROOT:
        parser.error("output must be a new directory under the repository, outside the reference release")
    if args.archive_only:
        if not (stage / "BUILD-INFO.json").is_file():
            parser.error("archive-only requires a staged package")
        finalize(stage)
        return
    if args.reference is None:
        parser.error("--reference is required when staging a package")
    reference = args.reference.resolve()
    if stage.is_relative_to(reference):
        parser.error("output must be outside the reference release")
    if stage.exists():
        parser.error("output already exists; select a new output or use --archive-only after verification")
    executable = ROOT / "bin/x64/Agent Debug SDL2/dosbox-x.exe"
    if not executable.is_file() or not reference.is_dir():
        parser.error("build Agent Debug SDL2|x64 and provide a valid reference release first")
    cache = ROOT / "artifacts/cache"
    cache.mkdir(parents=True, exist_ok=True)
    runtime_archive = cache / PYTHON_ARCHIVE
    if not runtime_archive.is_file():
        print(f"Downloading {PYTHON_URL}", flush=True)
        urllib.request.urlretrieve(PYTHON_URL, runtime_archive)
    if sha256(runtime_archive) != PYTHON_SHA256:
        raise RuntimeError("Portable Python archive hash does not match the pinned build input")
    wheels = cache / "wheels-cp313"
    wheels.mkdir(exist_ok=True)
    common = ["--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313", "--no-deps"]
    if not any(wheels.glob("mcp-1.26.0-*.whl")):
        subprocess.run([sys.executable, "-m", "pip", "download", "-r", str(ROOT / "client/python/requirements.lock"), "--dest", str(wheels), *common], check=True)

    print("Staging emulator, runtime assets, Python and adapter...", flush=True)
    stage.mkdir(parents=True)
    shutil.copy2(executable, stage / "dosbox-x.exe")
    for directory in ("drivez", "glshaders", "languages", "scripts", "shaders"):
        if (reference / directory).is_dir():
            shutil.copytree(reference / directory, stage / directory)
    for name in ("FREECG98.BMP", "Nouveau_IBM.ttf", "SarasaGothicFixed.ttf", "wqy_11pt.bdf", "wqy_12pt.bdf", "inpout32.dll", "inpoutx64.dll"):
        if (reference / name).is_file():
            shutil.copy2(reference / name, stage / name)
    for name in ("COPYING", "dosbox-x.reference.conf", "dosbox-x.reference.full.conf"):
        shutil.copy2(ROOT / name, stage / ("COPYING.txt" if name == "COPYING" else name))
    shutil.copy2(reference / "README.txt", stage / "README.upstream.txt")
    shutil.copy2(ROOT / "CHANGELOG", stage / "changelog.txt")
    for path in (ROOT / "packaging/windows").iterdir():
        if path.is_file() and path.name not in ("launch.py", "DOSDrive-README.txt"):
            shutil.copy2(path, stage / path.name)
    (stage / "adapter").mkdir()
    shutil.copytree(ROOT / "client/python/dosbox_agent", stage / "adapter/dosbox_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "packaging/windows/launch.py", stage / "adapter/launch.py")
    shutil.copy2(ROOT / "client/python/requirements.lock", stage / "adapter/requirements.lock")
    (stage / "DOSDrive").mkdir()
    shutil.copy2(ROOT / "packaging/windows/DOSDrive-README.txt", stage / "DOSDrive/README.txt")
    docs = stage / "docs"
    docs.mkdir()
    for name in ("mcp-agent-guide.md", "rpc-mcp-extensions.md", "windows-package.md",
                 "rpc-development.md", "rpc.md", "rpc-agent-usage.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        text = text.replace("../client/python/requirements.lock", "../adapter/requirements.lock")
        text = text.replace("../tests/agent/mcp-example.env", "../testing/mcp-example.env")
        (docs / name).write_text(text, encoding="utf-8")
    testing = stage / "testing"
    testing.mkdir()
    example = (ROOT / "tests/agent/mcp-example.env").read_text(encoding="utf-8")
    example = example.replace(r"..\..\bin\x64\Agent Debug SDL2\dosbox-x.exe", r"..\dosbox-x.exe")
    example = example.replace("dosbox_workdir=runtime-mcp", r"dosbox_workdir=fixtures")
    (testing / "mcp-example.env").write_text(example, encoding="utf-8")
    shutil.copy2(ROOT / "client/python/tests/mcp_e2e.py", testing / "mcp_e2e.py")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "tests/agent/build_fixture.ps1"),
                    "-RuntimeDirectory", str(testing / "fixtures")], check=True)
    python = stage / "python"
    with zipfile.ZipFile(runtime_archive) as bundle:
        bundle.extractall(python)
    packages = python / "Lib/site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "--target", str(packages),
                    "--no-compile", "-r", str(ROOT / "client/python/requirements.lock"), *common], check=True)
    (python / "python313._pth").write_text("python313.zip\n.\nLib\\site-packages\n..\\adapter\nimport site\n", encoding="utf-8")
    subprocess.run([str(python / "python.exe"), "-c", "import sys,mcp,uvicorn,dosbox_agent; print('Portable runtime:',sys.version)"], check=True)
    info = {
        "package": stage.name, "adapter_version": "0.2.0", "platform": "Windows x64", "configuration": "Agent Debug SDL2|x64",
        "built_at_utc": datetime.now(timezone.utc).isoformat(), "source_commit": git("rev-parse", "HEAD").decode().strip(),
        "includes_working_tree_changes": True, "emulator_sha256": sha256(executable), "python_version": PYTHON_VERSION,
        "python_download": PYTHON_URL, "python_archive_sha256": PYTHON_SHA256,
        "dependency_lock_sha256": sha256(ROOT / "client/python/requirements.lock"),
        "third_party_programs_included": False, "reference_layout": str(reference),
        "reference_executable_included": False, "reference_user_configuration_included": False,
        "wheel_hashes": {path.name: sha256(path) for path in sorted(wheels.glob("*.whl"))},
    }
    (stage / "BUILD-INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"Staged package: {stage}", flush=True)
    if not args.stage_only:
        finalize(stage)


if __name__ == "__main__":
    main()
