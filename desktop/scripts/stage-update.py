"""Stage public application resources for the existing installation; never replace its EXE."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"


def source_hashes():
    files = set()
    for name in ("xuloom", "tests", "scripts", "examples", "docs", "desktop/src", "desktop/electron", "desktop/tests", "desktop/scripts"):
        files.update(p for p in (ROOT/name).rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".tsx", ".ts", ".css", ".cjs", ".ps1"} and "__pycache__" not in p.parts)
    files.update(ROOT/name for name in ("setup.py", "pyproject.toml", "README.md", "README.en.md", "desktop/package.json", "desktop/package-lock.json"))
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-directory", type=Path, default=DESKTOP / "release/Xuloom-win32-x64")
    parser.add_argument("--validation-receipt", type=Path)
    args = parser.parse_args()
    sources = source_hashes()
    validation = None
    if args.validation_receipt:
        validation = json.loads(args.validation_receipt.read_text(encoding="utf-8"))
        if validation.get("passed") is not True or validation.get("sources") != sources:
            raise SystemExit("Validation receipt does not certify the current source")
        if validation.get('cleanSource') is True and (validation.get('sourceCommit') != subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
                or subprocess.check_output(['git','status','--porcelain','--untracked-files=normal'],cwd=ROOT).strip()):
            raise SystemExit('Clean validation no longer matches the candidate commit')
    package = args.package_directory.resolve()
    executable = package / "Xuloom.exe"
    if not executable.is_file() or not (package / "resources/python/python.exe").is_file():
        raise SystemExit("The existing installation and its bundled runtime are required")
    if not (DESKTOP / "dist/index.html").is_file():
        raise SystemExit("Run npm run build before staging resources")
    build_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    stage = DESKTOP / "build" / ("pending-update-general-" + build_id + "-" + uuid.uuid4().hex[:6])
    stage.mkdir(parents=True)
    dependencies = stage / "dependencies"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile", "--target", str(dependencies), "prompt_toolkit>=3.0,<4"], check=True)
    # setuptools copies incrementally; retired modules must not survive in wheels.
    cached = ROOT / "build/lib/xuloom"
    if cached.exists():
        if cached.resolve() != ROOT.resolve() / "build/lib/xuloom" or cached.is_symlink():
            raise SystemExit("Refusing a linked package build cache")
        shutil.rmtree(cached)
    wheels = stage / "wheels"; wheels.mkdir()
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
        "--wheel-dir", str(wheels), str(ROOT)], cwd=ROOT, check=True)
    wheel, = wheels.glob("xuloom-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        metadata, = {name.split("/")[0] for name in archive.namelist() if ".dist-info/" in name}
        for member in archive.infolist():
            relative = Path(member.filename)
            if member.is_dir(): continue
            if relative.is_absolute() or ".." in relative.parts:
                raise SystemExit("Invalid wheel member")
            if relative.parts[0] == "xuloom": target = stage / relative
            elif relative.parts[0] == metadata: target = stage / "metadata" / Path(*relative.parts[1:])
            else: raise SystemExit("Unexpected distribution root: " + relative.parts[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    source = ROOT / "xuloom"
    for pattern in ("*.py", "*.md"):
        expected = {path.relative_to(source) for path in source.rglob(pattern)}
        actual = {path.relative_to(stage / "xuloom") for path in (stage / "xuloom").rglob(pattern)}
        if expected != actual or any((source / path).read_bytes() != (stage / "xuloom" / path).read_bytes() for path in expected):
            raise SystemExit("Wheel resources do not match public source")
    skills = sorted(path.parent.name for path in (stage / "xuloom/skills/builtin").glob("*/SKILL.md"))
    if skills != ['code-development', 'code-review', 'data-analysis', 'debug-issue', 'document-summary', 'document-writing', 'experiment-design', 'skill-creator', 'web-research']:
        raise SystemExit("Unexpected bundled skills")
    app = stage / "app"; app.mkdir()
    for directory in ("dist", "electron"):
        shutil.copytree(DESKTOP / directory, app / directory)
    version = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["version"]
    if metadata != "xuloom-" + version + ".dist-info":
        raise SystemExit("Python distribution and application versions must match")
    (app / "package.json").write_text(json.dumps({"name": "xuloom-desktop", "productName": "Xuloom",
        "version": version, "main": "electron/main.cjs", "buildId": build_id}, indent=2), encoding="utf-8")
    if validation:
        shutil.copyfile(args.validation_receipt, app / "build-validation.json")
    subprocess.run([str(package / "resources/python/python.exe"), "-I", "-B", "-c",
        "import sys;sys.path[:0]=[sys.argv[1],sys.argv[1]+'/dependencies'];import xuloom,xuloom.core.session_mutation,xuloom.core.engine,xuloom.skills.terminal;from xuloom.protocols.mcp_bridge import MCP_AVAILABLE;assert MCP_AVAILABLE;assert xuloom.__version__==sys.argv[2];print('Staged engine, version and bundled dependencies verified')",
        str(stage), version], cwd=stage, check=True)
    manifest = {"metadataDirectory": metadata, "buildId": build_id, "pythonPackages": [p.name for p in dependencies.iterdir() if p.name != "__pycache__"],
        "originalExeSha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "bundledSkills": skills}
    if source_hashes() != sources: raise SystemExit("Source changed while staging")
    provenance = dict(sourceCommit=subprocess.check_output(["git", "rev-parse", "HEAD"],cwd=ROOT,text=True).strip(),
        sourceHashes=sources, sourceTreeSha256=hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest(),
        dirty=bool(subprocess.check_output(["git","status","--porcelain"],cwd=ROOT)),
        validationReceiptSha256=hashlib.sha256(args.validation_receipt.read_bytes()).hexdigest() if validation else None)
    (app/"build-provenance.json").write_text(json.dumps(provenance,indent=2),encoding="utf-8")
    manifest.update(**provenance, resourceHashes={p.relative_to(stage).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for directory in ("xuloom", "app", "metadata", "dependencies") for p in sorted((stage/directory).rglob("*")) if p.is_file() and "__pycache__" not in p.parts})
    if validation: shutil.copyfile(args.validation_receipt,stage/"validation-receipt.json")
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if list(stage.rglob("*.exe")): raise SystemExit("Resource updates must not contain another executable")
    print(json.dumps({"stage": str(stage), "target": str(package), **manifest}))


if __name__ == "__main__":
    main()
