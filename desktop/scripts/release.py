"""Archive verified resources with the existing runtime; never replace a running EXE."""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def archive_files(package, replacements=()):
    """Overlay complete resource trees in the ZIP, without mutating the installation."""
    trees = [(Path(target), Path(source)) for target, source in replacements]
    files = {}
    metadata = {target.name for target, _ in trees if target.name.startswith("xuloom-") and target.name.endswith(".dist-info")}
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if metadata and any(part.startswith("xuloom-") and part.endswith(".dist-info") and part not in metadata for part in relative.parts):
            continue
        if any(relative == target or target in relative.parents for target, _ in trees):
            continue
        files[relative] = path
    for target, source in trees:
        if target.is_absolute() or ".." in target.parts or not source.is_dir():
            raise ValueError("Invalid staged resource tree")
        if source.is_symlink() or getattr(source.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Refusing a linked staged resource tree")
        for path in source.rglob("*"):
            files[target / path.relative_to(source)] = path
    return sorted(files.items())


def verify_provenance(app, revision, sources):
    provenance = json.loads((app / 'build-provenance.json').read_text(encoding='utf-8'))
    payload = (app / 'build-validation.json').read_bytes()
    validation = json.loads(payload)
    if provenance.get('dirty') is not False or provenance.get('sourceCommit') != revision or provenance.get('sourceHashes') != sources:
        raise ValueError('Application provenance does not match the clean release commit')
    if validation.get('passed') is not True or validation.get('cleanSource') is not True or validation.get('sourceCommit') != revision or validation.get('sources') != sources:
        raise ValueError('Validation does not certify the release commit')
    if hashlib.sha256(payload).hexdigest() != provenance.get('validationReceiptSha256'):
        raise ValueError('Packaged validation hash does not match provenance')
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--staged-resources", type=Path, help="Use a validated resource update without changing the running installation")
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", args.tag):
        raise SystemExit("Use a version tag such as v0.1.0")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise SystemExit("Commit the release sources before creating a distributable")
    package = DESKTOP / "release/Xuloom-win32-x64"
    app = package / "resources/app"
    site = package / "resources/python/Lib/site-packages/xuloom"
    replacements = []
    if args.staged_resources:
        stage = args.staged_resources.resolve()
        if not stage.is_relative_to((DESKTOP / "build").resolve()):
            raise SystemExit("Staged resources must be inside desktop/build")
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        if hashlib.sha256((package / "Xuloom.exe").read_bytes()).hexdigest() != manifest["originalExeSha256"]:
            raise SystemExit("Staged resources belong to a different runtime")
        metadata_directory = manifest["metadataDirectory"]
        if not re.fullmatch(r"xuloom-[0-9A-Za-z_.-]+\.dist-info", metadata_directory):
            raise SystemExit("Invalid staged package metadata")
        app, site = stage / "app", stage / "xuloom"
        replacements = [(Path("resources/app"), app), (Path("resources/python/Lib/site-packages/xuloom"), site),
            (Path("resources/python/Lib/site-packages") / metadata_directory, stage / "metadata")]
        for dependency in manifest["pythonPackages"]:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dependency) or dependency in {"xuloom", metadata_directory}:
                raise SystemExit("Invalid staged dependency")
            replacements.append((Path("resources/python/Lib/site-packages") / dependency, stage / "dependencies" / dependency))
        if len({target for target, _ in replacements}) != len(replacements):
            raise SystemExit("Duplicate staged resource tree")
        if ({p.name for p in app.iterdir()} - {"dist", "electron", "package.json", "build-provenance.json", "build-validation.json"}
                or not {"dist", "electron", "package.json"} <= {p.name for p in app.iterdir()}):
            raise SystemExit("Unexpected staged application resource")
    metadata = json.loads((app / "package.json").read_text(encoding="utf-8"))
    if metadata["version"] != args.tag[1:]:
        raise SystemExit("Application resource version must match the release tag")
    revision = git('rev-parse', 'HEAD')
    verify_provenance(app, revision, runpy.run_path(str(DESKTOP/'scripts/stage-update.py'))['source_hashes']())
    # Reject old resources, missing new modules, and additional private skill payloads.
    for directory, installed, patterns in [
        (ROOT / "xuloom", site, ("*.py", "*.md")),
        (DESKTOP / "dist", app / "dist", ("*",)),
        (DESKTOP / "electron", app / "electron", ("*",)),
    ]:
        source = {p.relative_to(directory) for pattern in patterns for p in directory.rglob(pattern) if p.is_file()}
        actual = {p.relative_to(installed) for pattern in patterns for p in installed.rglob(pattern) if p.is_file()}
        if source != actual or any((directory / name).read_bytes() != (installed / name).read_bytes() for name in source):
            raise SystemExit("Installed resources do not match source: " + directory.name)
    if sorted(p.parent.name for p in (site / "skills/builtin").glob("*/SKILL.md")) != ['code-development', 'code-review', 'data-analysis', 'debug-issue', 'document-summary', 'document-writing', 'experiment-design', 'skill-creator', 'web-research']:
        raise SystemExit("Unexpected bundled skills")
    # Only known application roots may be published. User state stays outside this package.
    allowed = {p.name for p in (DESKTOP / "node_modules/electron/dist").iterdir()} - {"electron.exe"}
    allowed.update({"Xuloom.exe", "START-HERE.txt", "LICENSE-Xuloom.txt"})
    if any(p.name not in allowed for p in package.iterdir()):
        raise SystemExit("Unexpected file in portable installation")
    if {p.name for p in (package / "resources").iterdir()} != {"app", "python", "default_app.asar"}:
        raise SystemExit("Unexpected resource directory")
    output = DESKTOP / "release" / f"Xuloom-{args.tag}-windows-x64.zip"
    checksum = output.with_name("SHA256SUMS.txt")
    if output.exists():
        raise SystemExit("Release archive already exists; never overwrite a published build")
    record = {"tag": args.tag, "sourceCommit": revision, "platform": "windows-x64",
              "version": metadata["version"], "buildId": metadata.get("buildId"),
              "bundledSkills": ['code-development', 'code-review', 'data-analysis', 'debug-issue', 'document-summary', 'document-writing', 'experiment-design', 'skill-creator', 'web-research']}
    skipped = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative, path in archive_files(package, replacements):
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise SystemExit("Refusing a linked package path")
            if not path.is_file():
                continue
            # pip's generated launcher embeds the build interpreter's absolute path.
            local_launcher = relative.as_posix() == "resources/python/Lib/site-packages/bin/xuloom.exe"
            if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"} or path.name == "direct_url.json" or local_launcher:
                skipped += 1
                continue
            dependency_workspace = relative.as_posix().startswith("resources/python/Lib/site-packages/anthropic/")
            forbidden = {".git", ".xuloom", ".env", "history"}
            if not dependency_workspace:
                forbidden.add("workspaces")
            if any(part.lower() in forbidden for part in relative.parts):
                raise SystemExit("Refusing user/private data in archive")
            archive.write(path, "Xuloom-win32-x64/" + relative.as_posix())
        archive.writestr("Xuloom-win32-x64/RELEASE.json", json.dumps(record, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit("Archive integrity check failed: " + bad)
    hasher = hashlib.sha256()
    with output.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    checksum.write_text(digest + "  " + output.name + "\n", encoding="utf-8")
    print(json.dumps({"archive": str(output), "bytes": output.stat().st_size, "sha256": digest,
                      "checksum": str(checksum), "sourceCommit": revision, "excludedCachesAndLocalMetadata": skipped}))


if __name__ == "__main__":
    main()
