"""Validate a complete release ZIP and stage a Windows installation swap."""
import argparse
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from xuloom.core.storage import atomic_json
from xuloom.maintenance.archive import digest, safe_path


def stage_update(package, archive_path=None, *, rollback=None):
    package = Path(package).resolve()
    if not (package / 'Xuloom.exe').is_file():
        raise ValueError('Installed Xuloom.exe was not found')
    safe_path(package.parent, package.name)
    staging = package.parent / '.xuloom-updates'
    safe_path(package.parent, '.xuloom-updates')
    staging.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=staging))
    candidate = stage / 'next'
    candidate.mkdir()
    try:
        if rollback:
            previous = Path(rollback).resolve()
            if not previous.is_relative_to(staging.resolve()) or previous.name != 'previous':
                raise ValueError('Invalid rollback installation')
            for source in previous.rglob('*'):
                relative = source.relative_to(previous).as_posix()
                safe_path(previous, relative)
                target = safe_path(candidate, relative)
                if source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            version = json.loads((candidate / 'resources/app/package.json').read_text(encoding='utf-8'))['version']
        else:
            archive_path = Path(archive_path)
            checksums = archive_path.parent / 'SHA256SUMS.txt'
            lines = checksums.read_text(encoding='utf-8-sig').splitlines()
            expected = [line.split()[0].lower() for line in lines if len(line.split()) == 2 and line.split()[1].lstrip('*') == archive_path.name]
            if len(expected) != 1 or not re.fullmatch(r'[a-f0-9]{64}', expected[0]) or digest(archive_path) != expected[0]:
                raise ValueError('ZIP checksum mismatch; download ZIP and SHA256SUMS.txt from the same release')
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                if len(entries) > 50000 or sum(e.file_size for e in entries) > 2 * 1024**3:
                    raise ValueError('Release archive exceeds limits')
                roots = {e.filename.split('/')[0] for e in entries}
                if len(roots) != 1:
                    raise ValueError('Expected a complete Windows package directory')
                prefix = roots.pop() + '/'
                seen = set()
                for entry in entries:
                    relative = entry.filename[len(prefix):]
                    if not relative:
                        continue
                    target = safe_path(candidate, relative)
                    if relative.casefold() in seen or stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError('Duplicate or linked update entry')
                    seen.add(relative.casefold())
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as source, target.open('xb') as out:
                            shutil.copyfileobj(source, out)
            release = json.loads((candidate / 'RELEASE.json').read_text(encoding='utf-8'))
            version = json.loads((candidate / 'resources/app/package.json').read_text(encoding='utf-8'))['version']
            if release.get('version') != version or release.get('tag') != 'v' + version:
                raise ValueError('Release version metadata is inconsistent')
        for name in ('Xuloom.exe', 'resources/python/python.exe', 'resources/app/electron/main.cjs'):
            if not (candidate / name).is_file():
                raise ValueError('Release package is incomplete: ' + name)
        hashes = {p.relative_to(candidate).as_posix(): digest(p) for p in candidate.rglob('*') if p.is_file()}
        manifest = {'package': str(package), 'version': version, 'originalExeSha256': digest(package / 'Xuloom.exe'), 'hashes': hashes}
        atomic_json(stage / 'manifest.json', manifest)
        atomic_json(stage / 'status.json', {'state': 'staged', 'version': version})
        return {'stage': str(stage), 'version': version, 'rollback': str(stage / 'previous')}
    except BaseException:
        shutil.rmtree(stage)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    print(json.dumps(stage_update(args.package, args.archive, rollback=args.rollback)))


if __name__ == '__main__':
    main()
